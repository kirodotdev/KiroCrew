"""Fault-injection checks for the V4 prompt routing/turn ownership boundary."""
from __future__ import annotations

import json
from dataclasses import dataclass
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import kiro_byo.turn_runner as runner
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.paths import use_app_home
from kiro_byo.protocol import type_selection_id
from kiro_byo.runtime_trace import RuntimeTraceStore
from kiro_byo.session_events import set_route_pin


MODEL = ModelSpec('p', 'P', 'fast', 'Fast', routing_tiers=('fast', 'strong'), tool_use=True)
OTHER = ModelSpec('p', 'P', 'strong', 'Strong', routing_tiers=('fast', 'strong'), tool_use=True)
PROVIDER = ProviderSpec('p', 'P', 'openai-compatible', 'https://example.invalid/v1', '', (MODEL, OTHER))
CONFIG = KiroByoConfig((PROVIDER,), mode_pack='tiga', mode_type='multi')


def _response(output, req_id):
    return next(row for line in output.getvalue().splitlines()
                if (row := json.loads(line)).get('id') == req_id)


def test_checkpoint_failure_never_starts_durable_run_or_provider_and_releases_locks(tmp_path, monkeypatch):
    monkeypatch.setenv('KIRO_BYO_RUNTIME_TRACE', 'metadata')
    with use_app_home(tmp_path):
        output = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=output)
        session = Session('checkpoint-denied', str(tmp_path), CONFIG.public_model_id(MODEL))
        server._put_session(session)
        calls = []
        # Only the checkpoint made by the prompt should fail; _put_session has completed.
        server._persist_session = lambda s: False
        try:
            with patch.object(AcpServer, '_config', staticmethod(lambda: CONFIG)), \
                 patch.object(runner, 'chat_completion_turn', lambda *a, **kw: calls.append(a) or None):
                server._handle_prompt_request(10, {'sessionId': session.session_id,
                    'prompt': [{'type': 'text', 'text': 'do not call provider'}]})
            assert calls == []
            assert server._durable_run_store().list(session_id=session.session_id) == []
            assert not session.turn_lock.locked()
            assert session.active_turn is False
            assert session.active_turn_id == ''
            assert _response(output, 10)['result']['stopReason'] == 'end_turn'
            assert RuntimeTraceStore().recent(5, event='model.route.selected') == []
        finally:
            server._executor.shutdown(wait=True)


def test_route_failure_does_not_start_durable_run_or_invoke_provider(tmp_path, monkeypatch):
    monkeypatch.setenv('KIRO_BYO_RUNTIME_TRACE', 'metadata')
    with use_app_home(tmp_path):
        output = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=output)
        session = Session('route-denied', str(tmp_path), type_selection_id('tiga', 'multi'))
        server._put_session(session)
        calls = []
        # The injected route failure exercises the real ACP admission and failure events.
        seams = server._runtime_seams()
        monkeypatch.setattr(seams.model_router, 'route', lambda *a, **kw: None)
        try:
            with patch.object(AcpServer, '_config', staticmethod(lambda: CONFIG)), \
                 patch.object(runner, 'chat_completion_turn', lambda *a, **kw: calls.append(a) or None):
                server._handle_prompt_request(11, {'sessionId': session.session_id,
                    'prompt': [{'type': 'text', 'text': 'route me'}]})
            assert not calls
            assert server._durable_run_store().list(session_id=session.session_id) == []
            assert len(RuntimeTraceStore().recent(5, event='model.route.failed')) == 1
            assert RuntimeTraceStore().recent(5, event='model.route.selected') == []
            assert not session.turn_lock.locked() and session.active_turn is False
            assert _response(output, 11)['result']['stopReason'] == 'end_turn'
        finally:
            server._executor.shutdown(wait=True)


@dataclass(frozen=True)
class _Decision:
    provider: object
    model: object
    execution_intent: object
    execution_fallbacks: tuple
    pack: str = 'tiga'
    policy: str = 'glitter'
    selection_source: str = 'policy_ranked'


def test_sticky_type_pin_only_restores_eligible_model(tmp_path):
    with use_app_home(tmp_path):
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        session = Session('sticky-boundary', str(tmp_path), type_selection_id('tiga', 'glitter'))
        server._put_session(session)
        try:
            set_route_pin(session, 'tiga:glitter', 'p', 'strong', reason='test')
            sticky = SimpleNamespace(sticky=True)
            eligible = _Decision(PROVIDER, MODEL, sticky, (SimpleNamespace(provider=PROVIDER, model=OTHER),))
            updated, selected_provider, selected_model = server._restore_prompt_type_pin(session, eligible, PROVIDER, MODEL)
            assert selected_provider is PROVIDER
            assert selected_model is OTHER
            assert updated.selection_source == 'sticky_pin'
            # An old pin must not authorize a model outside the current executable pool.
            restricted = _Decision(PROVIDER, MODEL, sticky, (SimpleNamespace(provider=PROVIDER, model=MODEL),))
            updated, _, selected_model = server._restore_prompt_type_pin(session, restricted, PROVIDER, MODEL)
            assert selected_model is MODEL
            assert updated.selection_source == 'policy_ranked'
        finally:
            server._executor.shutdown(wait=True)
