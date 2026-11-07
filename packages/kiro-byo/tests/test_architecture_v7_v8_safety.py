"""V7/V8 fault-path contracts; never replay tools or stale recovered runs."""
from __future__ import annotations

from threading import Lock
from types import SimpleNamespace

from kiro_byo.acp_server import AcpServer
from kiro_byo.turn_runner import TurnRunnerMixin, _LoopDirective, _ProviderLoopState


def _state():
    return _ProviderLoopState(
        provider=None, model=None, mode_fallbacks=[], relay=None,
        turn_effort='auto', turn_thinking='auto', turn_run_id='turn', turn_trace_id='trace',
    )


def test_worker_guard_hands_back_without_tool_replay_or_final_failure():
    seen = []

    class Harness(TurnRunnerMixin):
        def _runtime_event(self, name, session, *, data):
            seen.append(('event', name))

        def _advance_turn_phase(self, session, turn_state, phase, *, reason):
            seen.append(('phase', reason))

    state = _state()
    state.step = 3
    state.relay = object()
    state.execution_guard_reason = 'stagnant'
    session = SimpleNamespace(session_id='s', state_lock=Lock())
    context = SimpleNamespace(role='worker', model=SimpleNamespace(model_id='worker-model'))
    direction = Harness()._handle_tool_guard_stop(session, state, context)
    assert direction is _LoopDirective.WORKER_GUARD_HAND_BACK
    assert state.relay is None and state.relay_next_role == 'lead'
    assert state.execution_guard_reason == ''
    assert not state.execution_guard_halted
    assert seen == [('event', 'model.relay.guard_handback'), ('phase', 'worker_guard_handback')]


def test_emergency_guard_is_terminal_without_dispatching_more_tools(monkeypatch):
    seen = []
    from kiro_byo import turn_runner
    monkeypatch.setattr(turn_runner, 'append_message', lambda sess, msg: seen.append(('message', msg['content'])))

    class Harness(TurnRunnerMixin):
        def _runtime_event(self, name, session, *, data):
            seen.append(('event', name, data['reason']))

        def _advance_turn_phase(self, session, state, phase, *, reason):
            seen.append(('phase', reason))

    state = _state()
    state.execution_guard_reason = 'emergency_tool_call_limit'
    session = SimpleNamespace(state_lock=Lock())
    context = SimpleNamespace(role='worker')
    direction = Harness()._handle_tool_guard_stop(session, state, context)
    assert direction is _LoopDirective.STOP
    assert state.execution_guard_halted
    assert len([x for x in seen if x[0] == 'message']) == 1
    assert ('event', 'execution.guard.stopped', 'emergency_tool_call_limit') in seen
    assert ('phase', 'execution_guard_stopped') in seen


def test_restore_reconciliation_skips_live_lease_and_raced_owner():
    pending = [
        {'run_id': 'risk', 'state': 'unknown_side_effect'},
        {'run_id': 'healthy', 'active_lease': True},
        {'run_id': 'raced', 'state': 'pending_restart'},
        {'run_id': 'clean', 'state': 'pending_restart'},
    ]
    calls = []

    class Store:
        def pending(self, *, session_id):
            assert session_id == 's'
            return pending

        def is_lease_active(self, record):
            return bool(record.get('active_lease'))

        def restart_recovery_state(self, record):
            return record['state']

        def mark_recovery(self, run_id, state):
            calls.append(('mark', run_id, state))
            if run_id == 'raced':
                raise ValueError('lease renewed')

    class Harness:
        def _durable_run_store(self):
            return Store()

        def _agent_state_store(self):
            return self

        def mark_session_stale(self, session_id):
            calls.append(('stale', session_id))
            return 1

        def _runtime_event(self, name, session, *, data):
            calls.append(('event', name, data))

        def trace(self, message):
            raise AssertionError('unexpected projection error')

    session = SimpleNamespace(events=[])
    AcpServer._reconcile_restored_durable_runs(Harness(), session, 's')
    assert ('mark', 'risk', 'unknown_side_effect') in calls
    assert not any(x[:2] == ('mark', 'healthy') for x in calls)
    assert ('mark', 'raced', 'pending_restart') in calls
    assert ('mark', 'clean', 'pending_restart') in calls
    events = [x for x in session.events if x['type'] == 'run/recovery_pending']
    assert len(events) == 1
    assert events[0]['data']['run_ids'] == ['risk', 'clean']
    assert not any('execute' in str(x).lower() or 'replay' in str(x).lower() for x in calls)


def test_restore_projection_failure_is_advisory_not_a_ledger_failure():
    class Harness:
        def _durable_run_store(self):
            return self

        def pending(self, *, session_id):
            return [{'run_id': 'risk'}]

        def is_lease_active(self, record):
            return False

        def restart_recovery_state(self, record):
            return 'unknown_side_effect'

        def mark_recovery(self, run_id, state):
            assert (run_id, state) == ('risk', 'unknown_side_effect')

        def _agent_state_store(self):
            return self

        def mark_session_stale(self, session_id):
            raise OSError('injected projection failure')

        def trace(self, message):
            assert 'OSError' in message

        def _runtime_event(self, name, session, *, data):
            assert data == {'runs': 1, 'stale_agents': 0}

    session = SimpleNamespace(events=[])
    AcpServer._reconcile_restored_durable_runs(Harness(), session, 's')
    assert session.events[-1]['data']['run_ids'] == ['risk']
