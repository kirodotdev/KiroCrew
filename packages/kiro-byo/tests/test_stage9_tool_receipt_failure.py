"""Stage 9: a post-dispatch receipt failure stops the turn without tool replay."""
from __future__ import annotations

from types import SimpleNamespace
from threading import Lock

from kiro_byo.turn_runner import (
    DurableCheckpointError, TurnRunnerMixin, _LoopDirective, _ProviderLoopState,
)


def test_post_execution_receipt_failure_does_not_continue_or_retry(monkeypatch):
    from kiro_byo import turn_runner as module
    monkeypatch.setattr(module, 'append_message', lambda *_: None)
    events = []

    class Harness(TurnRunnerMixin):
        def _advance_turn_phase(self, *a, **k):
            pass

        def _tool_effect_class(self, *a, **k):
            return 'unknown_write'

        def _checkpoint_durable_run(self, session, name, *, data=None, critical=False):
            events.append(('checkpoint', name, critical))
            if name == 'tool_batch_completed':
                raise DurableCheckpointError(name)

        def _runtime_seams(self):
            class Executor:
                def execute(self, *a, **k):
                    events.append(('tool', 'dispatched'))
                    return SimpleNamespace(
                        tool_ms=1, mcp_wait_ms=0, native_calls=0,
                        tool_search_calls=0, tool_search_loaded=0,
                        pending_replan_notice='', guard_reason='',
                        cancelled=False, stop_requested=False, steered=False,
                    )
            return SimpleNamespace(tool_executor=Executor())

        def _recover_current_turn(self, *a, **kw):
            events.append(('recovery', kw['preserve_side_effects']))

        def emit_runtime_error(self, *a, **k):
            events.append(('runtime_error',))

        def _record_provider_turn_telemetry(self, *a, **kw):
            events.append(('telemetry', kw['error_category']))

        def _provider_prompt_result(self, state, status):
            return {'stopReason': status}

        def result(self, request_id, payload):
            events.append(('response', request_id, payload['stopReason']))

    state = _ProviderLoopState(
        provider=None, model=None, mode_fallbacks=[], relay=None,
        turn_effort='auto', turn_thinking='auto', turn_run_id='turn', turn_trace_id='trace',
    )
    state.step = 1
    session = SimpleNamespace(
        state_lock=Lock(), active_run_id='run', active_run_step_id='step',
        session_id='session',
    )
    round_ctx = SimpleNamespace(role='lead', provider=SimpleNamespace(id='provider'),
                                model=SimpleNamespace(model_id='model'))
    call = SimpleNamespace(name='external_operation', call_id='call', arguments_text='{}')
    turn = SimpleNamespace(text='', provider_context=None, tool_calls=[call])
    direction = Harness()._handle_tool_round(
        'req', session, state, round_ctx, turn,
        history_start=0, t_prompt=0.0, routing_decision=None,
    )
    assert direction == _LoopDirective.RETURN
    assert events.count(('tool', 'dispatched')) == 1
    assert ('checkpoint', 'tool_batch_admitted', True) in events
    assert ('checkpoint', 'tool_batch_completed', True) in events
    assert ('recovery', True) in events
    assert ('response', 'req', 'error') in events
