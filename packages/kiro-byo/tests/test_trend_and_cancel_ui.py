"""Overview height regression and cancellation-source transparency.

Do not conflate a client cancellation notification with a manual user action;
ACP session/cancel may also be sent by the host on timeouts or handoffs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'ui' / 'index.mjs'
LOCALES = ROOT / 'src' / 'kiro_byo' / 'locales'


def test_overview_panel_uses_content_height_and_timeline_matches_svg_ratio():
    ui = UI.read_text(encoding='utf-8')
    assert '.ka-overview-trend-layout{grid-template-columns:minmax(0,2.05fr) minmax(240px,.95fr);flex:1 1 auto;align-items:stretch;min-height:0;min-width:0}' in ui
    assert 'const performancePanel=h(Panel,{' in ui
    assert 'title:tr("ui.f82648443829"),className:\'fill\'' not in ui
    assert 'const {width,height}=bounds' in ui
    assert '.ka-latency-chart-wrap>.ka-latency-chart{display:block;width:100%;height:100%;max-height:none' in ui
    assert 'hoursWithLatency===1' in ui
    assert 'new ResizeObserver(measure)' in ui
    assert '.ka-overview-trend-layout>.ka-panel:last-child>.ka-panel-body{overflow-y:auto' in ui
    assert '@container (max-width:730px)' in ui
    assert 'chart.single_hour_sample' in ui


def test_cancel_details_show_existing_telemetry_source_without_changing_status():
    ui = UI.read_text(encoding='utf-8')
    assert "selectedTurn?.cancel_source||cancelledEvent?.data?.source" in ui
    assert "e.event==='control.cancel.requested'" in ui
    assert "cancelOrigin==='client_session_cancel'" in ui
    assert 'records.cancel_origin_client_detail' in ui
    assert "selectedTurn.outcome==='cancelled'" in ui


def test_new_localizations_identical_with_matching_placeholders():
    keys = [
        'chart.single_hour_sample','records.cancel_origin',
        'records.cancel_origin_client','records.cancel_origin_client_detail',
        'records.cancel_origin_other','records.cancel_origin_unrecorded',
    ]
    total_keys = None
    for locale in ('zh-CN','zh-TW','en','ja','ko'):
        data = json.loads((LOCALES / f'{locale}.json').read_text(encoding='utf-8'))
        assert all(isinstance(data[key],str) and data[key].strip() for key in keys)
        assert set(re.findall(r'\{v\d+\}',data['records.cancel_origin_other'])) == {'{v0}'}
        assert all(not re.search(r'\{v\d+\}',data[key]) for key in keys if key != 'records.cancel_origin_other')
        if total_keys is None:
            total_keys = set(data)
        else:
            assert set(data)==total_keys


def test_cancelled_turn_does_not_inflate_successful_latency_trend(tmp_path):
    import time
    from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore
    store=TurnTelemetryStore(tmp_path / 'turns.jsonl')
    fields=dict(session_id='session',requested_model='x',provider_id='p',served_model='m',
        effort='',first_visible_ms=100,upstream_ms=0,tool_ms=0,mcp_wait_ms=0,
        tool_calls=0,native_calls=0,tool_search_calls=0,active_tools=0,
        tool_schema_bytes=0,stream_chunks=1,stream_chars=2)
    now=time.time()
    store.record(TurnTelemetry(timestamp=now,outcome='success',total_ms=3500,**fields))
    store.record(TurnTelemetry(timestamp=now,outcome='cancelled',total_ms=45100,**fields))
    snapshot=store.summary()
    active=[row for row in snapshot['hourly'] if row['requests']]
    assert len(active)==1
    assert active[0]['requests']==2
    assert active[0]['success']==1
    assert active[0]['avg_ms']==3500
    assert snapshot['last24']['avg_total_ms']==3500
    assert snapshot['last24']['requests']==2
