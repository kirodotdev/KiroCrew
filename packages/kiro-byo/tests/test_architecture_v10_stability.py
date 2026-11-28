"""V10 cross-boundary stability smoke checks (no live host/Provider claim)."""
from __future__ import annotations

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.overview_state import build_overview_snapshot
from kiro_byo.runtime_trace import RuntimeTraceStore
from kiro_byo.mode_registry import load_mode_registry
from kiro_byo.paths import use_app_home


def test_failed_request_is_visible_without_degrading_healthy_runtime(tmp_path):
    with use_app_home(tmp_path):
        ledger = DurableRunStore()
        active = ledger.create_run('healthy-session')
        risky = ledger.create_run('risky-session')
        ledger.checkpoint(
            risky['run_id'], name='tool_batch_admitted',
            data={'side_effect_risk': True},
        )
        traces = RuntimeTraceStore()
        traces.record('model.route.selected', session_id='healthy-session',
                      data={'pack': 'tiga', 'policy': 'multi'})
        snapshot = build_overview_snapshot(
            version='0.2.5', app_home=str(tmp_path), credential_backend='file',
            providers=[{'models': [{'id': 'test'}]}],
            runtime_capabilities={}, native_tools=[],
            integration={'supported': True, 'installed': True, 'active': True,
                         'restart_required': False, 'repairable': False},
            diagnostics={'turns': 2, 'success': 0, 'failed': 1, 'cancelled': 1},
        )
        assert snapshot['runtime_health']['state'] == 'ready'
        assert snapshot['runtime_health']['tone'] == 'success'
        assert snapshot['request_outcome']['failed'] == 1
        assert snapshot['request_outcome']['cancelled'] == 1
        assert [r['event'] for r in traces.recent(5, event='model.route.selected')] == ['model.route.selected']
        assert ledger.get(active['run_id']) is not None
        assert ledger.restart_recovery_state(ledger.get(risky['run_id'])) == 'unknown_side_effect'
        assert len(ledger.pending(session_id='risky-session')) == 1


def test_dyna_is_active_after_030_release():
    mode = load_mode_registry().get('dyna')
    assert mode is not None and mode.enabled is True
    assert mode.type_ids == ('miracle','flash','strong')
