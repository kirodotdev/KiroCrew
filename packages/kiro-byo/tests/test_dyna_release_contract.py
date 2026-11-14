"""0.3.0 launch contract: Dyna order, default, Mode isolation and real routes."""
from __future__ import annotations

import json
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_byo.turn_runner as runner
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import build_execution_plan, route_model
from kiro_byo.mode_registry import load_mode_registry
from kiro_byo.paths import use_app_home
from kiro_byo.policy_packs import get_policy_pack, public_policy_packs, resolve_policy
from kiro_byo.protocol import type_selection_id
from kiro_byo.provider_admin import mode_settings, set_mode_pack, set_mode_selection
from kiro_byo.providers.base import ChatTurn
from kiro_byo.routing_dashboard import build_routing_dashboard
from kiro_byo.runtime_trace import RuntimeTraceStore
from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore

TYPES = ('miracle', 'flash', 'strong')


def _cfg(pack='dyna', policy='flash'):
    fast = ModelSpec('p', 'P', 'fast', 'Fast', routing_tiers=('fast', 'balanced'),
                     routing_quality=.60, routing_speed=.98, routing_economy=.90, tool_use=True,
                     reasoning_supported=True, effort_supported_levels=('low', 'high'))
    strong = ModelSpec('p', 'P', 'strong', 'Strong', routing_tiers=('strong',),
                       routing_quality=.98, routing_speed=.40, routing_economy=.25, tool_use=True,
                       reasoning_supported=True, effort_supported_levels=('low', 'high', 'max'))
    provider = ProviderSpec('p', 'P', 'openai-compatible', 'https://example.invalid/v1', '', (fast, strong))
    return KiroByoConfig((provider,), mode_pack=pack, mode_type=policy)


def test_registry_order_and_defaults_are_independent():
    registry = load_mode_registry()
    assert registry.default().id == 'tiga'
    dyna = registry.get('dyna')
    assert dyna is not None and dyna.enabled
    assert dyna.type_ids == TYPES
    assert dyna.pack.default_policy == 'flash'
    assert [r['selection_id'] for r in next(p for p in public_policy_packs()['packs'] if p['id']=='dyna')['policies']] == [
        type_selection_id('dyna', p) for p in TYPES]
    assert get_policy_pack('tiga').default_policy == 'multi'


def test_mode_switch_resets_to_declared_default_without_overwriting_tiga(tmp_path):
    with use_app_home(tmp_path):
        assert mode_settings()['pack'] == 'tiga'
        assert set_mode_pack('dyna')['policy'] == 'flash'
        assert mode_settings()['pack'] == 'dyna'
        assert set_mode_selection('miracle', 'dyna')['policy'] == 'miracle'
        assert set_mode_pack('tiga')['policy'] == 'multi'
        assert set_mode_pack('dyna')['policy'] == 'flash'
        stored = json.loads((tmp_path/'config/providers.json').read_text('utf-8'))
        assert stored['mode'] == {'id': 'dyna', 'type': 'flash'}


@pytest.mark.parametrize('kind', TYPES)
def test_dyna_policy_is_bounded_and_uses_shared_router(kind):
    policy = resolve_policy('dyna', kind)[1]
    orchestration = policy.orchestration
    assert orchestration.single_writer is True
    assert orchestration.max_agents <= 6
    assert orchestration.max_parallel <= 4
    assert orchestration.max_depth <= 2
    assert policy.secondary is not None
    decision = route_model(_cfg(policy=kind), pack='dyna', policy=kind,
                           prompt='Analyze and review this repository', history=[], has_image=False,
                           tools_available=True)
    assert decision is not None
    assert decision.pack == 'dyna' and decision.policy == kind
    assert build_execution_plan(decision) is not None
    if kind == 'strong':
        assert decision.model.model_id == 'strong'


def test_mode_statistics_do_not_mix_tiga_and_dyna(tmp_path):
    telemetry = TurnTelemetryStore(tmp_path/'turns.jsonl')
    trace = RuntimeTraceStore(tmp_path/'trace.jsonl')
    common = dict(timestamp=time.time(), session_id='session', outcome='success',
                  provider_id='p', served_model='fast', effort='', total_ms=1000,
                  first_visible_ms=100, upstream_ms=900, tool_ms=0, mcp_wait_ms=0,
                  tool_calls=0, native_calls=0, tool_search_calls=0, active_tools=0,
                  tool_schema_bytes=0, stream_chunks=1, stream_chars=5, task_profile='code',
                  route_signals={'complexity':.6,'risk':.2})
    for mode, kind in [('tiga', 'multi'), ('dyna','miracle'), ('dyna','flash')]:
        telemetry.record(TurnTelemetry(**common, requested_model=type_selection_id(mode,kind),
                                      mode_pack=mode, mode_type=kind))
    result_dyna = build_routing_dashboard(_cfg(), telemetry=telemetry, trace=trace)
    assert result_dyna['pack'] == 'dyna'
    assert [r['id'] for r in result_dyna['policies']] == list(TYPES)
    assert {r['id']:r['recent_usage']['turns'] for r in result_dyna['policies']} == {
        'miracle':1,'flash':1,'strong':0}
    assert all(row['pack']=='dyna' for row in result_dyna['recent_routes'] if row['pack']=='dyna')
    result_tiga = build_routing_dashboard(_cfg('tiga','multi'), telemetry=telemetry, trace=trace)
    assert next(r for r in result_tiga['policies'] if r['id']=='multi')['recent_usage']['turns'] == 1


@pytest.mark.parametrize('kind', TYPES)
def test_dyna_executed_turn_emits_real_trace_and_keeps_run_correlated(tmp_path, monkeypatch,kind):
    monkeypatch.setenv('KIRO_BYO_RUNTIME_TRACE','metadata')
    with use_app_home(tmp_path):
        config = _cfg(policy=kind)
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        session = Session(f'dyna-{kind}', str(tmp_path), type_selection_id('dyna', kind))
        server._put_session(session)
        calls = []
        def provider_stub(provider, model_id, messages, emit, **kwargs):
            calls.append(model_id)
            emit('可控模拟推理成功')
            return ChatTurn(text='可控模拟推理成功')
        with patch.object(AcpServer, '_config', staticmethod(lambda: config)), \
             patch.object(runner, 'chat_completion_turn', provider_stub):
            try:
                server._handle_prompt_request(1, {'sessionId':session.session_id,
                    'prompt':[{'type':'text','text':'你好'}]})
            finally:
                server._executor.shutdown(wait=True)
        assert calls
        rows = [row for row in TurnTelemetryStore().recent(20) if row['session_id']==session.session_id]
        events = [event for event in RuntimeTraceStore().recent(20,event='model.route.selected')
                  if event['session_id']==session.session_id]
        assert len(rows)==len(events)==1
        row,event=rows[0],events[0]
        assert row['outcome']=='success'
        assert (row['mode_pack'],row['mode_type']) == ('dyna',kind)
        assert (event['data']['pack'],event['data']['policy'])==('dyna',kind)
        assert event['run_id'] == row['run_id'] and event['run_id']
        assert event['turn_id'] == row['turn_id'] and event['turn_id']


def test_multilingual_dyna_labels_and_ui_boundaries():
    root=Path(__file__).resolve().parents[1]
    keys={'mode.dyna.description','mode.dyna.flash.short','mode.dyna.strong.short','mode.dyna.miracle.short'}
    for name in ('zh-CN','zh-TW','en','ja','ko'):
        lang=json.loads((root/f'src/kiro_byo/locales/{name}.json').read_text('utf-8'))
        assert all(lang.get(key) for key in keys)
    ui=(root/'ui/index.mjs').read_text('utf-8')
    assert 'options:orderedPacks.map' in ui
    assert 'onClick:()=>setViewingType(pol.id)' in ui
    assert 'r.pack===packId&&r.policy===currentId' in ui
    assert 'routing?.pack===packId' in ui


def test_acp_conversation_picker_exposes_only_active_mode_type_order():
    current = type_selection_id('dyna','flash')
    config = _cfg()
    shared = AcpServer.models_payload(current, config, include_types=False)
    interactive = AcpServer.models_payload(current, config, include_types=True)
    shared_ids = [row['modelId'] for row in shared['availableModels']]
    interactive_ids = [row['modelId'] for row in interactive['availableModels']]
    assert not any(x.startswith('kiro-byo-type:') for x in shared_ids)
    assert [x for x in interactive_ids if x.startswith('kiro-byo-type:')] == [
        type_selection_id('dyna',kind) for kind in TYPES]
    assert not any('kiro-byo-type:tiga:' in x for x in interactive_ids)
    assert 'Fast' in interactive_ids and 'Strong' in interactive_ids
    tiga = AcpServer.models_payload(type_selection_id('tiga','multi'),
                                   _cfg('tiga','multi'), include_types=True)
    assert [x['modelId'] for x in tiga['availableModels'] if x['modelId'].startswith('kiro-byo-type:')] == [
        type_selection_id('tiga',kind) for kind in ('sky','multi','power','glitter')]


def test_dyna_strong_strict_pool_fails_closed_without_strong_models():
    fast = ModelSpec('only','Only','fast','Only fast',routing_tiers=('fast',),
                     routing_quality=.5,routing_speed=.9,routing_economy=.8,tool_use=True)
    config = KiroByoConfig((ProviderSpec('only','Only','openai-compatible',
                            'https://example.invalid/v1','',(fast,)),),
                            mode_pack='dyna',mode_type='strong')
    assert route_model(config, pack='dyna',policy='strong',prompt='hello',history=[],has_image=False) is None
