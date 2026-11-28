"""Regression: hidden session editors must not masquerade as an unsent draft.

Node VM exercises the actual injected script, not a rewritten JS imitation.
Does not claim Windows/Electron E2E coverage.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from kiro_byo.crew11_model_labels import block


@pytest.mark.skipif(shutil.which('node') is None, reason='Node unavailable')
@pytest.mark.parametrize('scenario,expected_reload,expected_block', [
    ('hidden_old_draft', 1, None),
    ('empty_visible_composer', 1, None),
    ('live_composer_draft', 0, 'draft'),
    ('visible_dialog_draft', 0, 'draft'),
    ('busy_with_hidden_old_draft', 0, 'busy'),
    ('focused_transparent_draft', 0, 'draft'),
])
def test_visible_editor_safety(tmp_path: Path, scenario: str,
                               expected_reload: int, expected_block: str | None) -> None:
    match = re.search(r'<script[^>]*>(.*?)</script>', block(), re.S)
    assert match is not None
    (tmp_path / 'actual.js').write_text(match.group(1), encoding='utf-8')
    (tmp_path / 'probe.cjs').write_text(r'''
const vm = require('node:vm'), fs = require('node:fs');
(async () => {
  const scenario = process.argv[3]; let poll, reloads = 0;
  const chip = {getAttribute:k => k==='data-model-id'?'kiro-byo-type:tiga:multi':null,
                textContent:''};
  const element = tag => ({tag,style:{},children:[],attrs:{},
    setAttribute(k,v){this.attrs[k]=v;},
    addEventListener(k,cb){this[k+'Handler']=cb;},
    appendChild(n){this.children.push(n);n.parentNode=this;},
    removeChild(n){this.children=this.children.filter(c=>c!==n);},
    querySelector(sel){return this.children.find(c=>c.attrs['data-kiro-byo-sync-message']!==undefined)||null;}});
  const hiddenEditor={value:'saved but hidden',hidden:false,
    checkVisibility:()=>false,getClientRects:()=>[],closest:()=>null};
  const emptyEditor={value:'',hidden:false,
    checkVisibility:()=>true,getClientRects:()=>[{}],closest:()=>null};
  const activeEditor={value:'user draft',hidden:false,
    checkVisibility:()=>true,getClientRects:()=>[{}],closest:()=>null};
  const editors = scenario==='focused_transparent_draft'?[hiddenEditor]:scenario==='hidden_old_draft' ? [hiddenEditor,emptyEditor] :
    scenario==='empty_visible_composer'?[emptyEditor] :
    scenario==='busy_with_hidden_old_draft'?[hiddenEditor,emptyEditor]:[activeEditor];
  const body=element('body');
  const document={body:null,visibilityState:'hidden',
    activeElement:scenario==='focused_transparent_draft'?hiddenEditor:null,
    addEventListener:()=>{},dispatchEvent:()=>{},createElement:element,
    querySelector:k=>k==='[data-testid="composer-model-chip"]'?chip:null,
    querySelectorAll:k=>k.startsWith('textarea,')?editors:[]};
  const window={setInterval:f=>{poll=f;return 1;},clearInterval:()=>{},
    addEventListener:()=>{},dispatchEvent:()=>{},requestAnimationFrame:()=>1,
    sessionStorage:{getItem:()=>null,setItem:()=>{}},location:{reload:()=>reloads++}};
  const fetch=async path=>{
    if(path==='/api/models')return {ok:true,json:async()=>[
      {model_name:'kiro-byo-type:dyna:miracle'},
      {model_name:'kiro-byo-type:dyna:flash'},
      {model_name:'kiro-byo-type:dyna:strong'}]};
    if(path==='/api/chat/slots')return {ok:true,json:async()=>[{
      running:scenario==='busy_with_hidden_old_draft',queue_depth:0}]};
    throw Error('unexpected request '+path);
  };
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),
    {window,document,fetch,Event:class{constructor(t){this.type=t;}},
     BroadcastChannel:class{constructor(){}},MutationObserver:class{observe(){}}});
  document.body=body;document.visibilityState='visible';poll();
  for(let i=0;i<24;i++)await new Promise(r=>setImmediate(r));
  console.log(JSON.stringify({reloads,blocked:window.__kiroByoModeSync?.blocked||null}));
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding='utf-8')
    r = subprocess.run(['node', str(tmp_path / 'probe.cjs'), str(tmp_path / 'actual.js'), scenario],
                       text=True, capture_output=True, timeout=15)
    assert r.returncode == 0, r.stdout + r.stderr
    import json
    actual = json.loads(r.stdout.strip().splitlines()[-1])
    assert actual == {'reloads': expected_reload, 'blocked': expected_block}
