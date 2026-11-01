"""Real CSS smoke test: KiroCrew-hosted overview uses a balanced two-column grid.

The test recreates the *outer* KiroCrew sidebar, narrow embedded app, and a
large number of providers. It never relies on screenshots or mocked CSS.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'ui' / 'index.mjs'


def _fixture(css: str, count: int, banner: bool = True) -> str:
    metrics = ''.join('<div class="ka-metric"><div class="k">状态</div><div class="v">2</div></div>' for _ in range(4))
    providers = ''.join(
        f'<div class="ka-provider-health-row"><div class="ka-provider-health-name">Provider {i}</div>'
        '<div class="ka-provider-health-meta">4 models<br>Provider transport</div></div>'
        for i in range(count)
    )
    alert = '<div class="ka-banner">集成待重启</div>' if banner else ''
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;height:100%;background:#2e3542}}
.host{{display:flex;height:100vh}}.crew-nav{{flex:0 0 282px}}
.host-stage{{flex:1;min-width:0;margin-top:52px;height:calc(100vh - 52px)}}
.ka-root{{--bg:#2e3542;--card:#313a49;--panel:#303847;--text:#e5eefc;--border:#49566a;--accent:#86c8dc}}
</style><style>{css}</style></head><body><div class="host"><div class="crew-nav"></div>
<div class="host-stage"><div class="ka-root"><div class="ka-app">
<aside class="ka-side"><div class="ka-brand">Kiro BYO</div></aside>
<main class="ka-main"><div class="ka-shell"><div class="ka-page-head"><h1>系统总览</h1></div>
<div class="ka-content"><div class="ka-page ka-overview-page">{alert}
<div class="ka-metrics metrics">{metrics}</div><div class="ka-overview-main ka-overview-trend-layout">
<section class="ka-panel"><div class="ka-panel-head"><h2>运行性能趋势</h2></div><div class="ka-panel-body"><div class="ka-overview-trends">
<div class="ka-overview-trend-metrics">{metrics}</div><div class="ka-overview-chart-head">每小时平均耗时</div>
<div class="ka-latency-chart-wrap"><svg class="ka-latency-chart" viewBox="0 0 600 310"><path d="M50 220L240 130L550 40"/></svg></div>
<div class="ka-overview-chart-axis">24 小时前 → 现在</div><div class="ka-overview-latencies">缓存命中率</div>
</div></div></section><section class="ka-panel"><div class="ka-panel-head"><h2>供应商状态</h2></div>
<div class="ka-panel-body"><div class="ka-provider-health-list">{providers}</div></div></section>
</div></div></div></div></main></div></div></div></body></html>'''


def test_overview_balanced_grid_and_internal_provider_scrolling():
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    chromium = shutil.which('chromium') or shutil.which('google-chrome')
    if not chromium:
        pytest.skip('Chromium is not available')
    source = SOURCE.read_text(encoding='utf-8')
    css = source.split('const CSS=`', 1)[1].split('`\n\nfunction injectStyles', 1)[0]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=chromium, args=['--no-sandbox'])
        try:
            for viewport in (1555, 1440, 1300, 1200, 1100):
                for provider_count in (2, 25):
                    page = browser.new_page(viewport={'width': viewport, 'height': 991})
                    try:
                        page.set_content(_fixture(css, provider_count))
                        result = page.evaluate('''() => {
                            const el = s => document.querySelector(s);
                            const grid = el('.ka-overview-trend-layout');
                            const [left,right] = grid.children;
                            const list = right.querySelector('.ka-panel-body');
                            const root = el('.ka-overview-page');
                            const chart = el('.ka-latency-chart-wrap');
                            return {
                              sameTop: Math.abs(left.getBoundingClientRect().top-right.getBoundingClientRect().top)<2,
                              sameHeight: Math.abs(left.clientHeight-right.clientHeight)<2,
                              leftWidth: left.clientWidth,
                              rightWidth: right.clientWidth,
                              chartHeight:chart.clientHeight,
                              innerScroll: list.scrollHeight>list.clientHeight+2,
                              outerScroll:root.scrollHeight>root.clientHeight+2,
                              horizontalOverflow: Math.max(0,root.scrollWidth-root.clientWidth,grid.scrollWidth-grid.clientWidth)
                            };
                        }''')
                        assert result['horizontalOverflow'] <= 1, (viewport, provider_count, result)
                        if viewport >= 1440:
                            assert result['sameTop'] and result['sameHeight'], result
                            assert result['leftWidth'] > result['rightWidth'] * 1.8, result
                            assert result['chartHeight'] >= 260, result
                            assert not result['outerScroll'], result
                            assert result['innerScroll'] == (provider_count > 2), result
                        elif provider_count > 2:
                            assert result['innerScroll'], result
                    finally:
                        page.close()
        finally:
            browser.close()
