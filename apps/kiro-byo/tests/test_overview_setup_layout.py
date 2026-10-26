"""Setup-page regression: cards must not stretch to the viewport bottom.

The browser fixture models the nested KiroCrew desktop sidebar, Kiro BYO rail,
and the shipping BYO CSS. No service configuration or requests are performed.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / 'ui' / 'index.mjs'


def _css() -> str:
    return UI.read_text(encoding='utf-8').split('const CSS=`', 1)[1].split('`\n\nfunction injectStyles', 1)[0]


def _fixture() -> str:
    steps = [
        ('① 添加供应商', '配置模型 API，并自动发现模型和能力。', '管理供应商'),
        ('② 检查 KiroCrew 集成', '确认 ACP Runtime 与 Dashboard seam 已正确部署。', '检查集成'),
        ('③ 启用 Agent Harness', 'Kiro BYO 已是新会话的 Agent Harness。', '已启用'),
    ]
    cards = ''.join(
        '<div class="ka-card soft ka-setup-step">'
        '<div class="ka-setup-step-copy">'
        f'<div class="ka-card-title">{title}</div>'
        f'<div class="ka-muted ka-setup-step-desc">{description}</div>'
        '</div><button class="ka-btn">'+ action + '</button></div>'
        for title, description, action in steps
    )
    return '''<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#2e3542}
.host{display:flex;height:100vh}.crew-nav{flex:0 0 282px}
.host-stage{flex:1;min-width:0;margin-top:52px;height:calc(100vh - 52px)}
.ka-root{--bg:#2e3542;--card:#313a49;--panel:#303847;--text:#e5eefc;--border:#49566a;--accent:#86c8dc;--accent-fg:#12212a}
</style><style>''' + _css() + '''</style></head><body><div class="host">
<div class="crew-nav"></div><div class="host-stage"><div class="ka-root"><div class="ka-app">
<aside class="ka-side"><div class="ka-brand">Kiro BYO</div></aside>
<main class="ka-main"><div class="ka-shell"><div class="ka-page-head"><div class="ka-page-copy"><h1>系统总览</h1><p>当前运行状态与基本配置</p></div></div>
<div class="ka-content"><div class="ka-page ka-overview-page">
<div class="ka-banner warn"><b>!</b> 集成需要安装或更新</div>
<div class="ka-metrics"><div class="ka-metric">供应商<br>2</div><div class="ka-metric">模型<br>6</div><div class="ka-metric">当前模式<br>Tiga</div><div class="ka-metric">KiroCrew<br>未就绪</div></div>
<div class="ka-overview-main ka-overview-setup"><section class="ka-panel ka-setup-panel">
<div class="ka-panel-head"><div><h2>开始使用</h2><p>Provider、Integration 与 Agent Harness 是三个独立状态；全部完成后才会显示 BYO 模型。</p></div></div>
<div class="ka-panel-body"><div class="ka-setup-steps">''' + cards + '''</div></div></section>
<section class="ka-panel"><div class="ka-panel-head"><h2>系统状态</h2></div><div class="ka-panel-body"><dl class="ka-kv">
<dt>管理接口</dt><dd>已连接</dd><dt>原生工具</dt><dd>14 个</dd><dt>会话能力</dt><dd>5 项</dd><dt>最近异常</dt><dd>0 个</dd>
</dl></div></section></div></div></div></div></main></div></div></div></body></html>'''


def test_setup_uses_compact_scoped_cards_and_preserves_actions():
    ui = UI.read_text(encoding='utf-8')
    assert "className:'ka-overview-main ka-overview-setup'" in ui
    assert "className:'ka-setup-panel'" in ui
    assert "className:'ka-setup-steps'" in ui
    assert ui.count("className:'ka-card soft ka-setup-step'") == 3
    assert "@container (max-width:580px)" in ui
    assert "@container (max-width:390px)" in ui
    assert "@container (max-width:700px)" in ui
    assert "onClick:()=>onNavigate('providers')" in ui
    assert ui.count("onClick:()=>onNavigate('integration')") >= 2
    setup = ui.split('const setupOverview=', 1)[1].split("return h('div',{className:'ka-page ka-overview-page'", 1)[0]
    assert "height:'100%'" not in setup
    assert "justifyContent:'space-between'" not in setup


def test_setup_cards_compact_and_responsive_in_embedded_crew():
    playwright = pytest.importorskip('playwright.sync_api')
    chromium = shutil.which('chromium') or shutil.which('google-chrome')
    if not chromium:
        pytest.skip('Chromium not installed')
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, executable_path=chromium, args=['--no-sandbox'])
        try:
            for width, expected_rows in ((1555, 1), (1280, 1), (1100, 2), (650, 3)):
                page = browser.new_page(viewport={'width': width, 'height': 991})
                try:
                    page.set_content(_fixture())
                    sizes = page.evaluate('''() => {
                      const p = s => document.querySelector(s), all = s => [...document.querySelectorAll(s)];
                      const cards = all('.ka-setup-step');
                      const bounds = cards.map(c => c.getBoundingClientRect());
                      const panel = p('.ka-setup-panel').getBoundingClientRect();
                      const status = p('.ka-overview-setup>.ka-panel:last-child').getBoundingClientRect();
                      const grid = p('.ka-overview-setup');
                      const overview = p('.ka-overview-page');
                      const cardRows = [...new Set(bounds.map(b => Math.round(b.top)))];
                      return {
                        rows:cardRows.length,
                        cardHeights:bounds.map(b=>b.height),
                        panelHeight:panel.height,
                        sameTop:Math.abs(panel.top-status.top)<2,
                        overlap:bounds.some((a,i)=>bounds.some((b,j)=>j>i && a.left<b.right-1 && b.left<a.right-1 && a.top<b.bottom-1 && b.top<a.bottom-1)),
                        overflow:Math.max(0, overview.scrollWidth-overview.clientWidth,grid.scrollWidth-grid.clientWidth),
                        buttonVisible:all('.ka-setup-step>.ka-btn').every(b=>b.clientHeight>=30),
                      };
                    }''')
                    assert sizes['rows'] == expected_rows, (width, sizes)
                    assert max(sizes['cardHeights']) < 250, (width, sizes)
                    assert sizes['panelHeight'] < 550, (width, sizes)
                    if width == 1555:
                        assert sizes['sameTop'], (width, sizes)
                    assert not sizes['overlap'], (width, sizes)
                    assert sizes['overflow'] <= 1, (width, sizes)
                    assert sizes['buttonVisible'], (width, sizes)
                    if width == 1555:
                        page.screenshot(path='/mnt/data/Kiro_BYO_0.2.3_总览布局预览.png', full_page=True)
                finally:
                    page.close()
        finally:
            browser.close()
