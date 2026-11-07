"""Dyna pre-release guardrails and Mode-neutral routing/records smoke tests.

Do not enable the reserved Dyna pack here: a custom enabled stand-in uses
identically named Types to prove shared infrastructure without inventing Dyna
routing semantics or touching Tiga's shipped policies.
"""
from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

import pytest

import kiro_byo.turn_runner as runner
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import route_model
from kiro_byo.mode_registry import load_mode_registry
from kiro_byo.paths import use_app_home
from kiro_byo.policy_packs import PolicyPackError, resolve_policy
from kiro_byo.protocol import type_selection_id
from kiro_byo.providers.base import ChatTurn
from kiro_byo.runtime_trace import RuntimeTraceStore
from kiro_byo.telemetry import TurnTelemetryStore


def _preflight_pack():
    return {
        'schema_version': 4, 'id': 'preflight', 'enabled': True,
        'display_name': 'Preflight Mode', 'default_policy': 'flash',
        'policies': [
            {'id': kind, 'semantics': {'display_name': f'{kind.title()} Type'},
             'execution': {'routing': {'quality': q, 'speed': s, 'economy': e}}}
            for kind, q, s, e in (
                ('flash', 0.34, 0.33, 0.33),
                ('strong', 0.70, 0.20, 0.10),
                ('miracle', 0.30, 0.50, 0.20),
            )
        ],
    }


def _config():
    models = (
        ModelSpec(provider_id='p', provider_name='P', model_id='fast', name='Fast',
                  routing_quality=0.45, routing_speed=0.98, routing_economy=0.95, tool_use=True),
        ModelSpec(provider_id='p', provider_name='P', model_id='strong', name='Strong',
                  routing_quality=0.98, routing_speed=0.35, routing_economy=0.25, tool_use=True),
    )
    return KiroByoConfig((ProviderSpec('p', 'P', 'openai-compatible', 'https://example.invalid', '', models),),
                         mode_pack='preflight', mode_type='flash')


def test_dyna_is_executable_and_uses_real_three_type_selection(tmp_path, monkeypatch):
    monkeypatch.setenv('KIRO_BYO_RUNTIME_TRACE', 'metadata')
    with use_app_home(tmp_path):
        config = KiroByoConfig(_config().providers, mode_pack='dyna', mode_type='flash')
        assert load_mode_registry().get('dyna').enabled is True
        for kind in ('miracle', 'flash', 'strong'):
            assert resolve_policy('dyna', kind)[1].id == kind
            decision = route_model(config, pack='dyna', policy=kind,
                                   prompt='hello', history=[], has_image=False)
            assert decision is not None and decision.policy == kind
            session = Session(f'dyna-{kind}', str(tmp_path), type_selection_id('dyna', 'flash'))
            server = AcpServer(stdin=StringIO(), stdout=StringIO())
            try:
                assert server._set_model(session, config, type_selection_id('dyna', kind))
                assert session.model == type_selection_id('dyna', kind)
            finally:
                server._executor.shutdown(wait=True)
        # Selection/config resolution is not an executed route event.
        assert RuntimeTraceStore().recent(10, event='model.route.selected') == []


def test_three_mode_neutral_types_share_real_route_and_run_trace(tmp_path, monkeypatch):
    monkeypatch.setenv('KIRO_BYO_RUNTIME_TRACE', 'metadata')
    with use_app_home(tmp_path):
        pack_path = tmp_path / 'policy_packs' / 'preflight.json'
        pack_path.parent.mkdir(parents=True)
        pack_path.write_text(json.dumps(_preflight_pack()), encoding='utf8')
        config = _config()
        calls = []

        def provider_stub(provider, model_id, messages, emit, **kwargs):
            calls.append(model_id)
            emit('模拟推理成功')
            return ChatTurn(text='模拟推理成功')

        for kind in ('flash', 'strong', 'miracle'):
            server = AcpServer(stdin=StringIO(), stdout=StringIO())
            session = Session(f'preflight-{kind}', str(tmp_path), type_selection_id('preflight', kind))
            server._put_session(session)
            with patch.object(AcpServer, '_config', staticmethod(lambda: config)), \
                 patch.object(runner, 'chat_completion_turn', provider_stub):
                try:
                    server._handle_prompt_request(1, {
                        'sessionId': session.session_id,
                        'prompt': [{'type': 'text', 'text': '回复你好'}],
                    })
                finally:
                    server._executor.shutdown(wait=True)
        assert len(calls) == 3
        rows = TurnTelemetryStore().recent(20)
        events = RuntimeTraceStore().recent(20, event='model.route.selected')
        assert len(rows) == len(events) == 3
        by_session = {r['session_id']: r for r in rows}
        for event in events:
            session_id = event['session_id']
            row = by_session[session_id]
            expected_type = session_id.removeprefix('preflight-')
            assert row['outcome'] == 'success'
            assert row['mode_pack'] == event['data']['pack'] == 'preflight'
            assert row['mode_type'] == event['data']['policy'] == expected_type
            assert row['run_id'] == event['run_id'] and event['run_id']
            assert row['turn_id'] == event['turn_id'] and event['turn_id']
            assert event['data']['resolved_model'] in {'fast', 'strong'}
            assert event['data']['selection']['routing_applied'] is True


def test_concrete_model_in_non_tiga_mode_does_not_fabricate_route(tmp_path, monkeypatch):
    monkeypatch.setenv('KIRO_BYO_RUNTIME_TRACE', 'metadata')
    with use_app_home(tmp_path):
        pack_path = tmp_path / 'policy_packs' / 'preflight.json'
        pack_path.parent.mkdir(parents=True)
        pack_path.write_text(json.dumps(_preflight_pack()), encoding='utf8')
        config = _config()
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        session = Session('preflight-direct', str(tmp_path), 'p/fast')
        # Public model IDs are resolved from actual provider catalog, not guessed.
        session.model = config.public_model_id(config.providers[0].models[0])
        server._put_session(session)

        def stub(provider, model_id, messages, emit, **kwargs):
            emit('直接调用成功')
            return ChatTurn(text='直接调用成功')

        with patch.object(AcpServer, '_config', staticmethod(lambda: config)), \
             patch.object(runner, 'chat_completion_turn', stub):
            try:
                server._handle_prompt_request(1, {
                    'sessionId': session.session_id,
                    'prompt': [{'type': 'text', 'text': '你好'}],
                })
            finally:
                server._executor.shutdown(wait=True)
        assert TurnTelemetryStore().recent(10)[0]['outcome'] == 'success'
        assert RuntimeTraceStore().recent(10, event='model.route.selected') == []
