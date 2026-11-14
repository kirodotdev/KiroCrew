"""Crew 0.11 RC1 model label bridge, separate from older dashboard guard."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from kiro_byo import crew11_model_labels as fix
from kiro_byo.crew_dashboard_patch import DashboardPatchError


def _dashboard(tmp_path: Path, *, code: str = 'data-model-id composer-model-chip data-setting-label data-multi-select-option agents.model') -> Path:
    package = tmp_path / 'kiro_crew'
    dist = package / 'static/dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<!doctype html><html><head></head><body>crew</body></html>')
    (dist / 'assets/hashed-file.js').write_text(code)
    return package


def test_names_distinguish_list_from_chat_and_keep_other_models_unchanged():
    labels = fix.display_labels()
    for form in ('sky','multi','power','glitter'):
        wire = f'kiro-byo-type:tiga:{form}'
        assert labels[wire]['short'] == f'{form.title()} Type'
        assert labels[wire]['full'] == f'Tiga · {form.title()} Type'
    js = fix.block()
    assert 'PREFIX = "kiro-byo-type:"' in js
    assert 'data-model-id' in js
    assert 'model-chip' in js
    assert 'onSelect' in js  # only explanatory comment
    assert "wire === 'auto:jev'" in js  # hide only in BYO chat picker
    assert 'settingsDefaultActive' in js
    assert 'data-multi-select-option' in js
    assert 'data-kiro-byo-crew11-type-first' in js
    # The deliberate mode-change fallback may reload ONLY after verifying all
    # Crew slots are idle; it must not rewrite React's model IDs.
    assert "fetch('/api/chat/slots'" in js
    assert "typeof slot.running === 'boolean'" in js
    assert 'window.location.reload()' in js
    assert 'window.fetch =' not in js


def test_discovery_is_idempotent_and_keeps_hashed_assets_untouched(tmp_path):
    package = _dashboard(tmp_path)
    js = package / 'static/dist/assets/hashed-file.js'
    before = js.read_bytes()
    first = fix.discover_dashboard_patches(package)[0]
    assert first.changed and first.path.name == 'index.html'
    assert first.patched.count(fix.START) == 1
    assert first.patched.count(fix.END) == 1
    first.path.write_text(first.patched)
    second = fix.discover_dashboard_patches(package)[0]
    assert not second.changed
    assert second.patched == first.patched
    assert js.read_bytes() == before


def test_upgrade_replaces_owned_guard_and_does_not_duplicate(tmp_path):
    package = _dashboard(tmp_path)
    first = fix.discover_dashboard_patches(package)[0]
    first.path.write_text(first.patched.replace('Sky Type','Sky Alpha'))
    upgraded = fix.discover_dashboard_patches(package)[0]
    assert upgraded.changed
    assert upgraded.patched.count(fix.START) == 1
    assert 'Sky Alpha' not in upgraded.patched
    assert 'Sky Type' in upgraded.patched


@pytest.mark.parametrize('code', ['data-model-id','composer-model-chip','data-model-id composer-model-chip', 'data-model-id composer-model-chip data-setting-label', 'data-model-id composer-model-chip data-setting-label data-multi-select-option', 'function foo(){}'])
def test_unknown_compiled_dom_contract_fails_closed(tmp_path, code):
    package = _dashboard(tmp_path, code=code)
    old = (package/'static/dist/index.html').read_bytes()
    with pytest.raises(DashboardPatchError, match='UI contract'):
        fix.discover_dashboard_patches(package)
    assert (package/'static/dist/index.html').read_bytes() == old


def test_bad_marker_fails_closed(tmp_path):
    package = _dashboard(tmp_path)
    index = package/'static/dist/index.html'
    index.write_text('<head>' + fix.START + '</head>')
    with pytest.raises(DashboardPatchError, match='incomplete or duplicated'):
        fix.discover_dashboard_patches(package)


def test_browser_script_syntax_is_valid(tmp_path):
    shell = fix.block()
    js = re.search(r'<script[^>]*>(.*?)</script>', shell, re.S).group(1)
    path = tmp_path/'labels.js'
    path.write_text(js)
    r = subprocess.run(['node','--check',str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_display_payload_cannot_break_out_of_script():
    js = fix.block({'kiro-byo-type:sample:unsafe':{
        'short':'</script><script>alert(1)</script>',
        'full':'" & <script>bad</script>',
    }})
    assert js.count('</script>') == 1
    assert '<script>alert' not in js
    assert '\\u003c/script' in js


def test_executable_dom_model_labels_and_selection_identity(tmp_path):
    """Exercise the injected script with the actual 0.11 DOM anchors mocked."""
    shell = fix.block()
    js = re.search(r'<script[^>]*>(.*?)</script>', shell, re.S).group(1)
    js_path = tmp_path/'labels.js'
    js_path.write_text(js)
    harness = r'''
const vm = require('vm');
const fs = require('fs');
const wire = 'kiro-byo-type:tiga:sky';
let menuWire = wire;
const menuText = {nodeType:3,nodeValue:wire};
const menuSpan = {
  getAttribute: k => k === 'data-model-id' ? menuWire : '',
  closest: k => k.includes('role=') ? {} : null,
  childNodes: [menuText], firstChild: menuText
};
const chipText = {nodeType:3,nodeValue:wire};
const chipSpan = {childNodes:[chipText],firstChild:chipText};
const attrs = {'title':'模型：' + wire,'aria-label':'模型：' + wire};
const chip = {querySelector:k => k === 'span.truncate' ? chipSpan:null,
  getAttribute:k => attrs[k]||'', setAttribute:(k,v)=>{attrs[k]=v;}};
const statusParent = {closest:()=>null};
const status = {nodeType:3,nodeValue:wire,parentElement:statusParent};
const document = {
  body: {},
  addEventListener:()=>{},
  querySelectorAll: s => s.includes('data-model-id') ? [menuSpan] : [],
  querySelector: s => s === '[data-testid="composer-model-chip"]'? chip:null,
  createTreeWalker: () => ({nextNode:(()=>{let used=false;return ()=>{if(used)return null;used=true;return status;};})()})
};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),{
  document,window:{requestAnimationFrame:fn=>fn(),setInterval:()=>1,clearInterval:()=>{},addEventListener:()=>{}},
  NodeFilter:{SHOW_TEXT:4},MutationObserver:class{observe(){}}
});
if(menuText.nodeValue!=='Sky Type')throw Error('menu label '+menuText.nodeValue);
if(menuWire!==wire)throw Error('wire identity mutated');
if(chipText.nodeValue!=='Tiga · Sky Type')throw Error('chat chip');
if(status.nodeValue!=='Tiga · Sky Type')throw Error('bottom status');
if(attrs.title!=='模型：Tiga · Sky Type' || attrs['aria-label']!=='模型：Tiga · Sky Type')throw Error('accessible label');
'''
    harness_path = tmp_path/'dom-test.cjs'
    harness_path.write_text(harness)
    ran = subprocess.run(['node',str(harness_path),str(js_path)], capture_output=True,text=True)
    assert ran.returncode == 0, ran.stderr


def test_real_crew11_picker_and_settings_dom_seams(tmp_path):
    """Run a stateful contract fixture for chat and Settings surface isolation."""
    shell = fix.block()
    script = re.search(r'<script[^>]*>(.*?)</script>', shell, re.S).group(1)
    script_path = tmp_path / 'display.js'
    script_path.write_text(script)
    fixture = Path(__file__).with_name('fixtures') / 'crew11_picker_settings_fixture.cjs'
    r = subprocess.run(['node',str(fixture),str(script_path)],capture_output=True,text=True)
    assert r.returncode == 0, r.stderr
    assert 'auto isolation' in r.stdout


def test_mate_model_picker_short_labels_and_wire_identity(tmp_path):
    """The two Mate 2.0 editors share agents.model, not Settings' model control."""
    shell = fix.block()
    script = re.search(r'<script[^>]*>(.*?)</script>', shell, re.S).group(1)
    js_path = tmp_path / 'mate-display.js'
    js_path.write_text(script)
    fixture = Path(__file__).with_name('fixtures') / 'crew11_mate_model_fixture.cjs'
    result = subprocess.run(['node', str(fixture), str(js_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Mate model trigger' in result.stdout


def test_missing_current_app_timing_asset_is_actionable_and_does_not_modify_crew(tmp_path):
    package = _dashboard(tmp_path)
    index = package / 'static/dist/index.html'
    before = index.read_bytes()
    with pytest.raises(DashboardPatchError, match='dashboard resource missing or unreadable'):
        fix.discover_dashboard_patches(package, timing_asset=tmp_path / 'no-such.js')
    assert index.read_bytes() == before
