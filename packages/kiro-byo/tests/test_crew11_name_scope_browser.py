"""Browser regressions for context-specific Crew display-only Type labels.

These DOM simulations cannot replace Windows Crew RC5 E2E validation.
"""
from __future__ import annotations

import shutil

import pytest

from kiro_byo.crew11_model_labels import block


def _page():
    api = pytest.importorskip('playwright.sync_api')
    executable = shutil.which('chromium') or shutil.which('chromium-browser')
    if not executable:
        pytest.skip('Chromium missing')
    pw = api.sync_playwright().start()
    browser = pw.chromium.launch(headless=True, executable_path=executable,
                                 args=['--no-sandbox'])
    page = browser.new_page()
    return pw, browser, page


def test_chat_pin_action_uses_short_type_but_chip_and_wire_stay_intact():
    pw, browser, page = _page()
    wire = 'kiro-byo-type:tiga:multi'
    try:
        page.set_content(f'''<!doctype html><html><head>{block()}</head><body>
        <button id="chip" data-testid="composer-model-chip" aria-expanded="true"
          title="模型：{wire}"><span class="truncate">{wire}</span></button>
        <div id="picker" role="dialog"><div>
          <button role="option"><span data-model-id="kiro-byo-type:tiga:sky">Sky Type</span></button>
          <button role="option"><span data-model-id="{wire}">{wire}</span></button>
          <button role="option"><span data-model-id="mimo-v2.6-flash">mimo-v2.6-flash</span></button>
        </div>
        <button id="pin" onclick="window.pinCount=(window.pinCount||0)+1">将 Tiga · Multi Type 置顶到 kirocrew 代理（覆盖全局默认）</button>
        </div><button id="unrelated-pin">将 Tiga · Multi Type 置顶到另一个区域</button><p id="status">{wire}</p></body></html>''')
        page.wait_for_function('''() => document.getElementById('pin').textContent.includes('将 Multi Type 置顶')''')
        assert page.locator('#unrelated-pin').inner_text() == '将 Tiga · Multi Type 置顶到另一个区域'
        assert page.locator('#chip .truncate').inner_text() == 'Tiga · Multi Type'
        assert page.locator('#status').inner_text() == 'Tiga · Multi Type'
        assert page.locator('[data-model-id="kiro-byo-type:tiga:multi"]').inner_text() == 'Multi Type'
        assert page.locator('[data-model-id="kiro-byo-type:tiga:multi"]').get_attribute('data-model-id') == wire
        page.locator('#pin').click()
        assert page.evaluate('window.pinCount') == 1
        # React may render the old phrase again on a subsequent update.
        page.evaluate('document.querySelector("#pin").firstChild.nodeValue = "将 Tiga · Multi Type 置顶到 kirocrew 代理（覆盖全局默认）"')
        page.wait_for_function('''() => document.getElementById('pin').textContent.includes('将 Multi Type 置顶')''')
    finally:
        browser.close(); pw.stop()


def test_mate_model_is_short_and_inherit_and_concrete_models_unchanged():
    pw, browser, page = _page()
    wire = 'kiro-byo-type:tiga:sky'
    try:
        page.set_content(f'''<!doctype html><html><head>{block()}</head><body>
        <button data-ui-location="agents.model" role="combobox" aria-expanded="true" aria-controls="mate-options">
          {wire}</button>
        <div id="mate-options" role="listbox">
          <div role="option" data-value="{wire}">{wire}</div>
          <div role="option" data-value="kiro-byo-type:tiga:multi">kiro-byo-type:tiga:multi</div>
          <div role="option" data-value="mimo-v2.6-flash">mimo-v2.6-flash</div>
          <div role="option" data-value="inherit">继承</div>
        </div></body></html>''')
        assert page.locator('[data-ui-location="agents.model"]').inner_text() == 'Sky Type'
        assert page.locator('[data-value="kiro-byo-type:tiga:sky"]').inner_text() == 'Sky Type'
        assert page.locator('[data-value="kiro-byo-type:tiga:multi"]').inner_text() == 'Multi Type'
        assert page.locator('[data-value="kiro-byo-type:tiga:sky"]').get_attribute('data-value') == wire
        assert page.locator('[data-value="inherit"]').inner_text() == '继承'
        assert page.locator('[data-value="mimo-v2.6-flash"]').inner_text() == 'mimo-v2.6-flash'
    finally:
        browser.close(); pw.stop()


@pytest.mark.parametrize('has_field_seam', [True, False])
@pytest.mark.parametrize('field_name', ['后台模型', '後台模型', 'Background Model', 'バックグラウンドモデル', '백그라운드 모델'])
def test_settings_background_model_shows_full_name_without_mutating_value(has_field_seam, field_name):
    pw, browser, page = _page()
    wire = 'kiro-byo-type:tiga:multi'
    attr = f'data-setting-label="{field_name}"' if has_field_seam else ''
    try:
        page.set_content(f'''<!doctype html><html><head>{block()}</head><body>
          <section id="background" {attr}><label>{field_name}</label>
            <button id="background-trigger" role="combobox" aria-expanded="false"
              onclick="this.setAttribute('aria-expanded','true'); window.setTimeout(() => {{
                document.querySelector('#portal').innerHTML = `<div role='option' data-value='{wire}'>{wire}</div><div role='option' data-value='auto'>auto</div>`;
              }},10)">{wire}</button>
          </section><div id="portal" role="listbox"></div>
          <div id="unrelated">{wire}</div>
        </body></html>''')
        page.wait_for_function('''() => document.getElementById('background-trigger').textContent === 'Tiga · Multi Type' ''')
        assert page.locator('#unrelated').inner_text() == wire
        page.locator('#background-trigger').click()
        page.wait_for_function('''() => document.querySelector('#portal [role="option"]') &&
          document.querySelector('#portal [role="option"]').textContent === 'Tiga · Multi Type' ''')
        assert page.locator('#portal [role="option"]').first.get_attribute('data-value') == wire
        assert page.locator('#portal [data-value="auto"]').inner_text() == 'auto'
    finally:
        browser.close(); pw.stop()


@pytest.mark.parametrize('heading', ['遥测', '遙測', 'Telemetry', 'テレメトリー', '텔레메트리'])
def test_developer_telemetry_full_labels_but_raw_log_and_user_code_untouched(heading):
    pw, browser, page = _page()
    wire = 'kiro-byo-type:tiga:multi'
    try:
        page.set_content(f'''<!doctype html><html><head>{block()}</head><body>
          <main><h1>{heading}</h1><h2>额度</h2><div id="bar">{wire}</div>
          <h2>上下文窗口</h2><table><thead><tr><th>模型</th></tr></thead>
          <tbody><tr><td id="model-col">{wire}</td></tr>
          <tr><td id="concrete">mimo-v2.6-flash</td></tr></tbody></table>
          <div id="unknown">unknown</div><div id="auto">auto</div>
          <pre><code id="raw">{wire}</code></pre>
          <p class="msg" id="user">{wire}</p></main></body></html>''')
        page.wait_for_function('''() => document.getElementById('bar').textContent==='Tiga · Multi Type' ''')
        assert page.locator('#model-col').inner_text() == 'Tiga · Multi Type'
        assert page.locator('#concrete').inner_text() == 'mimo-v2.6-flash'
        assert page.locator('#unknown').inner_text() == 'unknown'
        assert page.locator('#auto').inner_text() == 'auto'
        assert page.locator('#raw').inner_text() == wire
        assert page.locator('#user').inner_text() == wire
    finally:
        browser.close(); pw.stop()


def test_no_telemetry_heading_does_not_rewrite_general_plain_text():
    pw, browser, page = _page()
    wire = 'kiro-byo-type:tiga:multi'
    try:
        page.set_content(f'<!doctype html><html><head>{block()}</head><body><div id="id">{wire}</div></body></html>')
        assert page.locator('#id').inner_text() == wire
    finally:
        browser.close(); pw.stop()
