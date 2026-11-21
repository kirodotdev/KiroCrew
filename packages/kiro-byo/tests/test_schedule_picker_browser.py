"""Optional Chromium regression for KiroCrew's task editor model list.

The model dropdown mounts *after* the plain-DIV scheduler editor opens.  That
order is important: Type rows without data-model-id must still be hidden in the
scheduler, yet remain available from Chat when the overlay closes.
"""
from __future__ import annotations

import shutil

import pytest

from kiro_byo.crew_dashboard_patch import guard_block


def test_delayed_schedule_options_do_not_leak_into_chat_type_picker() -> None:
    pw = pytest.importorskip("playwright.sync_api")
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if not chromium:
        pytest.skip("Chromium not installed; DOM test requires headless Chromium")
    labels = {
        "kiro-byo-type:tiga:sky": {"full": "Tiga — Sky", "type": "Sky Type", "group": "形态"},
        "kiro-byo-type:tiga:multi": {"full": "Tiga — Multi", "type": "Multi Type", "group": "形态"},
        "kiro-byo-type:tiga:power": {"full": "Tiga — Power", "type": "Power Type", "group": "形态"},
        "kiro-byo-type:tiga:glitter": {"full": "Tiga — Glitter", "type": "Glitter Type", "group": "形态"},
    }
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        + guard_block(labels)
        + '</head><body><button id="chat-pick" role="option"><span data-model-id="kiro-byo-type:tiga:sky">Sky Type</span></button></body></html>'
    )
    with pw.sync_playwright() as api:
        browser = api.chromium.launch(headless=True, executable_path=chromium, args=["--no-sandbox"])
        try:
            page = browser.new_page()
            page.set_content(html)
            page.evaluate("document.documentElement.classList.add('kiro-byo-chat-surface')")
            # The editor is a normal div, NOT role=dialog, just like the Insider build.
            page.evaluate('''() => {
              document.body.insertAdjacentHTML('beforeend', `
                <div id="task-editor"><h2>每日新闻早报</h2>
                  <div><span>Cron 表达式</span><input value="0 10 * * *" /></div>
                  <div><span>时区</span><select><option>Asia/Shanghai</option></select></div>
                  <div><span>代理</span><button>kirocrew</button></div>
                  <div><span>模型</span><button id="open-models">Open model list</button></div>
                </div>`);
            }''')
            page.wait_for_function("document.documentElement.classList.contains('kiro-byo-task-editor-open')")
            # A separate user event adds a portal with plain-text rows.
            page.evaluate('''() => {
              const portal=document.createElement('div');portal.id='task-options';
              portal.innerHTML=`<div role="listbox">
                <button role="option">Sky Type</button><button role="option">Multi Type</button>
                <button role="option">Power Type</button><button role="option">Glitter Type</button>
                <button role="option">DeepSeek-V4.1-Flash</button></div>`;
              document.body.appendChild(portal);
            }''')
            page.wait_for_function("document.querySelector('#task-options [role=option]').hasAttribute('inert')")
            rows = page.eval_on_selector_all('#task-options [role=option]', '''els=>els.map(el=>({label:el.textContent.trim(),display:getComputedStyle(el).display,inert:el.hasAttribute('inert')}))''')
            assert [row["label"] for row in rows if row["display"] == "none"] == [
                "Sky Type", "Multi Type", "Power Type", "Glitter Type"
            ]
            assert rows[-1]["display"] != "none" and not rows[-1]["inert"]
            # Closing both portals restores only the actual conversation Type picker.
            page.evaluate("document.querySelector('#task-editor').remove();document.querySelector('#task-options').remove()")
            page.wait_for_function("!document.documentElement.classList.contains('kiro-byo-task-editor-open')")
            assert page.eval_on_selector('#chat-pick', '(el)=>getComputedStyle(el).display') != 'none'
            # A message containing task words should never qualify as an editor.
            page.evaluate('''() => document.body.insertAdjacentHTML('beforeend',
              '<article class="prose"><span>Cron 表达式</span><span>时区</span><span>模型</span><input/><textarea></textarea></article>')''')
            page.wait_for_timeout(60)
            assert not page.evaluate("document.documentElement.classList.contains('kiro-byo-task-editor-open')")
        finally:
            browser.close()
