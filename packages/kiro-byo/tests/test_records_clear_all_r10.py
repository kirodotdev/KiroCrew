"""The Records page must clear its own three backends, not only Turn telemetry."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.paths import use_app_home
from kiro_byo.record_center import clear_record_history, records_snapshot
from kiro_byo.runtime_trace import RuntimeTraceStore
from kiro_byo.telemetry import TurnTelemetryStore

from test_record_center import _turn
from test_records_health_regression import _app_routes, _Request


def _populate(home: Path):
    with use_app_home(home):
        TurnTelemetryStore().record(_turn())
        traces = RuntimeTraceStore()
        traces.record('model.route.selected', session_id='s1', data={'pack': 'dyna'})
        traces.record('tool.call.started', session_id='s1')
        ledger = DurableRunStore()
        completed = ledger.create_run('completed')
        ledger.finish_run(completed['run_id'], status='completed')
        active = ledger.create_run('active')
        failed = ledger.create_run('failed')
        ledger.finish_run(failed['run_id'], status='failed', recovery_state='retryable')
        uncertain = ledger.create_run('uncertain')
        ledger.finish_run(uncertain['run_id'], status='completed', recovery_state='unknown_side_effect')
        return completed, active, failed, uncertain


def test_clear_all_history_and_preserve_unsafe_runs(tmp_path, monkeypatch):
    monkeypatch.setenv('KIRO_BYO_RUNTIME_TRACE', 'metadata')
    completed, active, failed, uncertain = _populate(tmp_path)
    with use_app_home(tmp_path):
        assert records_snapshot()['summary']['routing'] == 1
        result = clear_record_history()
        assert result['removed'] == {'turns': 1, 'trace_events': 2, 'safe_runs': 1}
        assert result['retained_protected_runs'] == 3
        ledger = DurableRunStore()
        assert ledger.get(completed['run_id']) is None
        for row in (active, failed, uncertain):
            assert ledger.get(row['run_id']) is not None
        after = records_snapshot()
        assert after['summary']['turns'] == 0
        assert after['summary']['routing'] == 0
        assert after['summary']['runtime'] == 0
        assert after['summary']['protected_runs'] == 3
        assert RuntimeTraceStore().recent(10) == []
        assert TurnTelemetryStore().recent(10) == []
        repeat = clear_record_history()
        assert repeat['removed'] == {'turns': 0, 'trace_events': 0, 'safe_runs': 0}
        assert repeat['retained_protected_runs'] == 3


def test_http_delete_records_isolated_from_request_only_clear(tmp_path, monkeypatch):
    monkeypatch.setenv('KIRO_BYO_RUNTIME_TRACE', 'metadata')
    for routes in _app_routes():
        monkeypatch.setattr(routes, '_ensure_app_state', lambda ctx: tmp_path)
        routes_registered = [args[:2] for args, _ in routes.register_routes(object())]
        assert ('DELETE', '/records') in routes_registered
        _populate(tmp_path)
        response = asyncio.run(routes.clear_records(_Request(), None))
        assert response.status == 200
        body = json.loads(response.text)
        assert body['cleared'] is True
        assert body['removed']['trace_events'] == 2
        assert body['retained_protected_runs'] == 3
        with use_app_home(tmp_path):
            assert records_snapshot()['summary']['routing'] == 0
            assert records_snapshot()['summary']['protected_runs'] == 3


def test_ui_clear_history_uses_unified_endpoint_and_five_locales():
    project = Path(__file__).resolve().parents[1]
    ui = (project / 'ui' / 'index.mjs').read_text(encoding='utf-8')
    page = ui.split('function RecordsPage(', 1)[1].split('function SettingsPage(', 1)[0]
    assert "api('/records',{method:'DELETE'})" in page
    assert "api('/diagnostics',{method:'DELETE'})" not in page
    assert "'records.center.clear_protected_notice'" in page
    assert "'records.center.clear_protected_limited'" in page
    assert "h('button',{className:'ka-btn danger small',disabled:busy,onClick:clear},tr('records.center.clear_all'))" in page
    for lang in ('zh-CN', 'zh-TW', 'en', 'ja', 'ko'):
        translations = json.loads((project / 'src' / 'kiro_byo' / 'locales' / f'{lang}.json').read_text(encoding='utf-8'))
        for key in ('clear_all', 'clear_confirm', 'clear_protected_notice', 'clear_protected_limited', 'clear_scope'):
            assert translations['records.center.' + key]


def test_all_records_read_endpoints_converge_after_unified_clear(tmp_path, monkeypatch):
    """The page fetches four views: all must agree after the DELETE completes."""
    monkeypatch.setenv('KIRO_BYO_RUNTIME_TRACE', 'metadata')
    for routes in _app_routes():
        monkeypatch.setattr(routes, '_ensure_app_state', lambda ctx: tmp_path)
        _populate(tmp_path)
        asyncio.run(routes.clear_records(_Request(), None))
        snapshots = {}
        for name, handler in (
            ('records', routes.get_record_center),
            ('diagnostics', routes.get_diagnostics),
            ('runtime_trace', routes.get_runtime_trace),
            ('work_traces', routes.get_work_traces),
        ):
            result = asyncio.run(handler(_Request(limit='100'), object()))
            assert result.status == 200, (name, result.text)
            snapshots[name] = json.loads(result.text)
        assert snapshots['records']['summary']['turns'] == 0
        assert snapshots['records']['summary']['routing'] == 0
        assert snapshots['records']['summary']['runtime'] == 0
        assert snapshots['diagnostics']['turns'] == []
        assert snapshots['runtime_trace']['events'] == []
        assert snapshots['records']['summary']['protected_runs'] == 3
        # Crew projections still see protected execution evidence, rather than
        # mistaking a history deletion for cancellation or successful recovery.
        assert snapshots['work_traces']['summary']['windowSaturated'] is False
