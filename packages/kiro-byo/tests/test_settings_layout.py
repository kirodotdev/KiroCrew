"""The embedded KiroCrew settings UI must not require horizontal panning."""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'ui' / 'index.mjs'


class SettingsLayoutTests(unittest.TestCase):
    def test_settings_columns_can_shrink(self) -> None:
        js = SOURCE.read_text(encoding='utf-8')
        self.assertIn('.ka-settings .ka-setting-row{grid-template-columns:minmax(0,.85fr) minmax(0,1fr)', js)
        self.assertIn('.ka-settings .ka-kv dt,.ka-settings .ka-kv dd{overflow-wrap:anywhere}', js)
        self.assertIn('@media(max-width:1240px){.ka-settings{grid-template-columns:minmax(0,1fr)', js)
        self.assertNotIn('grid-template-columns:minmax(240px,.8fr) minmax(220px,1fr)', js.split('.ka-settings .ka-setting-row{',1)[1].split('}',1)[0])

    def test_chromium_settings_integration_has_no_horizontal_scroll(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright not installed (optional UI smoke test)')
        browser_path = shutil.which('chromium') or shutil.which('google-chrome') or shutil.which('google-chrome-stable')
        if not browser_path:
            self.skipTest('Chromium not installed (optional UI smoke test)')
        source = SOURCE.read_text(encoding='utf-8')
        css = source.split('const CSS=`', 1)[1].split('`\n\nfunction injectStyles', 1)[0]
        markup = '''<!doctype html><html><head><meta charset="utf-8"><style>
        html,body{margin:0;height:100%;background:#f1f3f7}
        .host{display:flex;height:100vh}.host-sidebar{flex:0 0 282px}
        .stage{flex:1;min-width:0;height:calc(100vh - 52px);margin-top:52px;zoom:1.2}
        </style><style>''' + css + '''</style></head><body><div class="host">
        <div class="host-sidebar"></div><div class="stage"><div class="ka-root"><div class="ka-app">
        <aside class="ka-side"><div class="ka-brand">Kiro BYO</div></aside>
        <main class="ka-main"><div class="ka-shell"><div class="ka-page-head"><h1>设置</h1></div>
        <div class="ka-content"><div class="ka-page"><div class="ka-settings">
        <section class="ka-panel fill"><div class="ka-panel-head"><h2>设置分类</h2></div>
        <div class="ka-panel-body"><div class="ka-setting-nav">
        <button>常规</button><button>推理与语言</button><button>恢复</button>
        <button>数据与记录</button><div class="ka-setting-nav-divider"></div>
        <button>KiroCrew</button><button>ACP 兼容性</button></div></div></section>
        <section class="ka-panel fill"><div class="ka-panel-head"><h2>KiroCrew</h2></div>
        <div class="ka-panel-body"><div class="ka-scroll">
        <div class="ka-card soft"><div class="ka-between"><strong>KiroCrew 0.10.0-insider.4</strong>
        <span class="ka-chip">尚未集成</span></div><div class="ka-sep"></div><dl class="ka-kv">
        <dt>Integration</dt><dd>需要安装 / 更新</dd>
        <dt>Agent Harness</dt><dd>Kiro BYO</dd>
        <dt>Runtime</dt><dd>0.2.2 → 0.2.2</dd>
        <dt>App Data</dt><dd>已绑定</dd>
        <dt>兼容 Profile</dt><dd>0.10.0 adaptive registry/capabilities (composed chat API owners)</dd>
        <dt>Source audit</dt><dd>0.10.0-insider.4 (e9bfada)</dd></dl></div>
        <div class="ka-setting-row"><div><div class="name">集成状态</div>
        <div class="desc">安装 Kiro BYO Integration 并绑定当前 KiroCrew。</div></div>
        <button class="ka-btn primary">处理中...</button></div>
        <div class="ka-setting-row"><div><div class="name">卸载集成</div>
        <div class="desc">移除 BYO Runtime，保留配置。</div></div>
        <button class="ka-btn">卸载集成</button></div>
        <div class="ka-setting-row"><div><div class="name">清除全部数据</div>
        <div class="desc">卸载集成并清除配置。</div></div>
        <button class="ka-btn danger">卸载并清除数据</button></div>
        </div></div></section></div></div></div></main>
        </div></div></div></body></html>'''
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=browser_path, args=['--no-sandbox'])
            try:
                for viewport in (1555, 1440, 1300, 1200, 1100):
                    with self.subTest(viewport=viewport):
                        page = browser.new_page(viewport={'width': viewport, 'height': 991})
                        page.set_content(markup)
                        overflow = page.locator('.ka-settings .ka-scroll').evaluate('(el) => el.scrollWidth - el.clientWidth')
                        self.assertLessEqual(overflow, 1, f'Horizontal overflow at viewport {viewport}: {overflow}px')
                        page.close()
            finally:
                browser.close()
