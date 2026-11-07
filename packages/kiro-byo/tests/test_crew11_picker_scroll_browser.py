"""Browser geometry regression for the Crew 0.11 chat model picker.

Playwright/Chromium are optional so the existing CI remains usable without a
browser installation; this test is valuable for interactive desktop fixes.
"""
from __future__ import annotations

import shutil

import pytest

from kiro_byo.crew11_model_labels import block


def _html(*, reused_portal: bool = False, stale_restoration: bool = False, delayed_model_row: bool = False) -> str:
    types = ''.join(
        f'<button role="option"><span data-model-id="kiro-byo-type:tiga:{name}">{name.title()} Type</span></button>'
        for name in ('sky', 'multi', 'power', 'glitter')
    )
    models = ''.join(
        f'<button role="option" aria-selected="{str(i == 1).lower()}">'
        f'<span data-model-id="model-{i}">Model {i}</span></button>'
        for i in range(1, 26)
    )
    return f'''<!doctype html><html><head><meta charset="utf-8">
    <style>
    body {{ min-height: 800px; font-family: sans-serif; }}
    #composer {{ position: fixed; bottom: 18px; right: 40px; }}
    #menu {{ position: fixed; bottom: 50px; right: 40px; max-height: 300px;
      width: 310px; overflow-y: auto; border: 1px solid #888; background: white; }}
    #menu button {{ display: flex; align-items: center; min-height: 36px;
      width: 100%; text-align: left; }}
    </style>{block()}</head><body>
    <div id="composer"><button id="chip" data-testid="composer-model-chip"
      aria-expanded="false"><span class="truncate">Tiga · Multi Type</span></button></div>
    <script>
    var chip = document.querySelector('#chip');
    document.addEventListener('click', function(evt) {{
      var row = evt.target.closest && evt.target.closest('#menu button[role="option"]');
      if (!row) return;
      var entry = row.querySelector('[data-model-id]');
      var value = entry.getAttribute('data-model-id');
      var title = entry.textContent;
      chip.querySelector('span.truncate').textContent = value.startsWith('kiro-byo-type:')
        ? 'Tiga · ' + title : title;
      var open = document.getElementById('menu');
      if ({str(reused_portal).lower()}) open.style.display = 'none';
      else open.remove();
      chip.setAttribute('aria-expanded', 'false');
    }});
    chip.addEventListener('click', function() {{
      var old = document.getElementById('menu');
      if (old && {str(reused_portal).lower()}) {{
        var show = old.style.display === 'none';
        old.style.display = show ? 'block' : 'none';
        if (show) {{
          old.scrollTop = 40; // Simulate Crew restoring a stale offset
          if ({str(stale_restoration).lower()}) window.setTimeout(() => {{
            if (old.style.display !== 'none') old.scrollTop = 0;
          }}, 110);
          if ({str(delayed_model_row).lower()}) {{
            let last = old.querySelector('[data-model-id="model-25"]').closest('button');
            if (last) {{
              last.remove();
              window.setTimeout(() => old.querySelector('#rows').appendChild(last), 110);
            }}
          }}
        }}
        chip.setAttribute('aria-expanded', String(show));
        return;
      }}
      if (old) {{ old.remove(); chip.setAttribute('aria-expanded','false'); return; }}
      chip.setAttribute('aria-expanded','true');
      requestAnimationFrame(function() {{
        var content = document.createElement('div');
        content.innerHTML = `<div id="menu" role="listbox"><div><input placeholder="Search models"></div>
           <div id="rows">{types}{models}</div></div>`;
        var menu = content.firstElementChild;
        document.body.appendChild(menu);
        menu.scrollTop = 40; // Simulate Crew restoring a stale offset on mount
      }});
    }});
    </script></body></html>'''


@pytest.mark.parametrize('reuse', [False, True])
def test_selected_model_reopens_visible_without_stealing_manual_scroll(reuse):
    api = pytest.importorskip('playwright.sync_api')
    chromium = shutil.which('chromium') or shutil.which('chromium-browser')
    if not chromium:
        pytest.skip('optional Chromium executable not installed')
    try:
        with api.sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=chromium,
                                         args=['--no-sandbox'])
            try:
                page = browser.new_page(viewport={'width': 1000, 'height': 740})
                page.set_content(_html(reused_portal=reuse))
                page.locator('#chip').click()
                page.wait_for_selector('#menu')
                # Even if Crew starts with a stale scroll offset and Multi is
                # already visible, reopening must show the heading from top.
                page.wait_for_function('''() => document.querySelector('#menu') &&
                   document.querySelector('#menu').scrollTop === 0''')
                assert page.locator('#menu').evaluate('e=>e.scrollTop') == 0
                # Actually choose the bottom row, then reopen as a user would.
                page.locator('#menu [data-model-id="model-25"]').click()
                assert page.locator('#chip span.truncate').inner_text() == 'Model 25'
                page.locator('#chip').click()
                page.wait_for_function('''() => {
                  let m=document.querySelector('#menu');
                  return m && m.scrollTop > 200;
                }''')
                row = page.locator('#menu [data-model-id="model-25"]')
                assert row.is_visible()
                assert page.evaluate('''() => {
                  let host=document.querySelector('#menu').getBoundingClientRect();
                  let chosen=document.querySelector('[data-model-id="model-25"]').getBoundingClientRect();
                  return chosen.top >= host.top && chosen.bottom <= host.bottom;
                }''')
                assert page.locator('#menu [data-model-id="kiro-byo-type:tiga:multi"]').count() == 1
                assert page.locator('#menu [data-kiro-byo-crew11-type-first]').count() == 1
                # User scrolling is never snapped back by a later mutation.
                page.locator('#menu').dispatch_event('wheel', {'deltaY': -70})
                page.locator('#menu').evaluate('(node) => { node.scrollTop = 0; }')
                page.locator('#menu').evaluate('node => node.setAttribute("aria-label", "updated")')
                page.wait_for_timeout(100)
                assert page.locator('#menu').evaluate('node=>node.scrollTop') == 0
                # Selected model changes to a Type near the top; the next open
                # should NOT jump down to the first concrete model.
                page.locator('#chip span.truncate').evaluate('(el)=>el.textContent="Tiga · Multi Type"')
                page.locator('#chip').click()
                page.locator('#chip').click()
                page.wait_for_function('''() => {
                   let m=document.querySelector('#menu');
                   return m && m.style.display !== 'none';
                }''')
                page.wait_for_timeout(100)
                page.wait_for_function('''() => document.querySelector('#menu').scrollTop === 0''')
                assert page.locator('#menu').evaluate('node=>node.scrollTop') == 0
                # The Type group heading should be above the selected Multi.
                assert page.locator('#menu').evaluate('''menu => {
                   let row = menu.querySelector('[data-kiro-byo-crew11-type-first]');
                   return !!row && row.getBoundingClientRect().top >= menu.getBoundingClientRect().top;
                }''')
                # Switch back to the last model and ensure a repeated open
                # positions the selected last row again.
                page.locator('#chip span.truncate').evaluate('(el)=>el.textContent="Model 25"')
                page.locator('#chip').click()
                page.locator('#chip').click()
                page.wait_for_function('''() => {
                  let m=document.querySelector('#menu');
                  return m && m.scrollTop > 200;
                }''')
            finally:
                browser.close()
    except Exception as exc:
        # A missing browser dependency is different from a functional failure.
        if 'Executable doesn\'t exist' in str(exc):
            pytest.skip('optional browser dependencies unavailable')
        raise


@pytest.mark.parametrize('selected_type', ['sky', 'multi', 'power', 'glitter'])
@pytest.mark.parametrize('reuse', [False, True])
def test_reopened_type_starts_at_group_top_even_with_stale_scroll(selected_type, reuse):
    """Crew's retained scroll must not hide the Type caption, especially Multi."""
    api = pytest.importorskip('playwright.sync_api')
    chromium = shutil.which('chromium') or shutil.which('chromium-browser')
    if not chromium:
        pytest.skip('optional Chromium executable not installed')
    with api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=chromium,
                                     args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1000, 'height': 740})
            html = _html(reused_portal=reuse).replace(
                'Tiga · Multi Type</span></button></div>',
                f'Tiga · {selected_type.title()} Type</span></button></div>',
            )
            page.set_content(html)
            page.locator('#chip').click()
            page.wait_for_selector('#menu')
            page.wait_for_function('''() => {
              let menu = document.querySelector('#menu');
              return menu && menu.scrollTop === 0;
            }''')
            assert page.locator('#menu').evaluate('el=>el.scrollTop') == 0
            # The caption is anchored to the first Type row, so it stays visible.
            assert page.locator('#menu').evaluate('''menu => {
              let first = menu.querySelector('[data-kiro-byo-crew11-type-first]');
              return first && first.getBoundingClientRect().top >= menu.getBoundingClientRect().top;
            }''')
            # Reopen after Crew restores a stale offset.
            page.locator('#chip').click()
            page.locator('#chip').click()
            page.wait_for_function('''() => {
              let menu = document.querySelector('#menu');
              return menu && menu.scrollTop === 0;
            }''')
            assert page.locator('#menu').evaluate('el=>el.scrollTop') == 0
        finally:
            browser.close()


@pytest.mark.parametrize('scenario', ['late_scroll_reset', 'late_model_row'])
def test_concrete_model_remains_visible_when_reused_portal_settles_late(scenario):
    """Crew can reuse the portal and settle its list after the opening event."""
    api = pytest.importorskip('playwright.sync_api')
    chromium = shutil.which('chromium') or shutil.which('chromium-browser')
    if not chromium:
        pytest.skip('optional Chromium executable not installed')
    with api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=chromium,
                                     args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1000, 'height': 740})
            page.set_content(_html(
                reused_portal=True,
                stale_restoration=scenario == 'late_scroll_reset',
                delayed_model_row=scenario == 'late_model_row',
            ))
            page.locator('#chip').click()
            page.locator('#menu [data-model-id="model-25"]').click()
            assert page.locator('#chip span.truncate').inner_text() == 'Model 25'
            page.locator('#chip').click()
            page.wait_for_timeout(360)
            assert page.evaluate('''() => {
              let host = document.querySelector('#menu').getBoundingClientRect();
              let chosen = document.querySelector('[data-model-id="model-25"]').getBoundingClientRect();
              return chosen.height > 0 && chosen.top >= host.top && chosen.bottom <= host.bottom;
            }'''), scenario
        finally:
            browser.close()


def test_last_concrete_model_mimo_pro_reopens_at_selected_row():
    """Reported Crew picker case: the selected footer model is below viewport."""
    api = pytest.importorskip('playwright.sync_api')
    chromium = shutil.which('chromium') or shutil.which('chromium-browser')
    if not chromium:
        pytest.skip('optional Chromium executable not installed')
    with api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=chromium,
                                     args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1000, 'height': 740})
            html = _html(reused_portal=True, stale_restoration=True).replace(
                'data-model-id="model-25">Model 25',
                'data-model-id="mimo-v2.6-pro">mimo-v2.6-pro',
            )
            page.set_content(html)
            page.locator('#chip').click()
            page.locator('#menu [data-model-id="mimo-v2.6-pro"]').click()
            assert page.locator('#chip span.truncate').inner_text() == 'mimo-v2.6-pro'
            page.locator('#chip').click()
            page.wait_for_timeout(400)
            assert page.evaluate('''() => {
                let m=document.querySelector('#menu').getBoundingClientRect();
                let r=document.querySelector('[data-model-id="mimo-v2.6-pro"]').getBoundingClientRect();
                return r.height > 0 && r.top >= m.top && r.bottom <= m.bottom;
            }''')
        finally:
            browser.close()
