"""R21 regression: same Mode, newly discovered relay models missing in chat.

Host DOM VM, not live Windows KiroCrew. Explicitly test refusal to overwrite the
user's saved Selectable Models preference and bounded refresh on an old menu.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_byo.crew11_model_labels import block


@pytest.mark.skipif(shutil.which('node') is None, reason='node unavailable')
@pytest.mark.parametrize('scenario', ['provider_added', 'draft', 'active_turn', 'saved_filter'])
def test_provider_catalogue_changes_without_mode_change(tmp_path: Path, scenario: str) -> None:
    m = re.search(r'<script[^>]*>(.*?)</script>', block(), re.S)
    assert m, 'injected script missing'
    js = tmp_path / 'host.js'
    js.write_text(m.group(1), encoding='utf-8')
    harness = tmp_path / 'host.cjs'
    harness.write_text(r'''
const fs=require('node:fs'),vm=require('node:vm');
(async()=>{
  const source=fs.readFileSync(process.argv[2], 'utf8');
  const scenario=process.argv[3];
  const store=new Map();
  const calls={reloads:0,models:0,slots:0};
  let now=100000;
  let available=['original-a','original-b'];
  let visible=['original-a','original-b'];
  let running=false,draft=false;
  const typeOption={getAttribute:(k)=>k==='data-model-id'?'kiro-byo-type:tiga:multi':'',closest:()=>({})};
  const chip={getAttribute:(k)=>k==='data-model-id'?'kiro-byo-type:tiga:multi':'',textContent:'Multi Type'};
  const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
  function mount(){
    let tick;
    const window={
      setInterval:fn=>{tick=fn;return 1;}, clearInterval:()=>{},
      addEventListener:()=>{}, dispatchEvent:()=>{}, requestAnimationFrame:()=>1,
      sessionStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)},
      location:{reload:()=>calls.reloads++}
    };
    const document={body:null,visibilityState:'visible',
      addEventListener:()=>{},dispatchEvent:()=>{},
      querySelector:(key)=>key==='[data-testid="composer-model-chip"]'?chip:null,
      querySelectorAll:(key)=>{
        if(key==='[data-model-id^="kiro-byo-type:"]')return [typeOption];
        if(key==='[role="option"] [data-model-id]')return [typeOption,...visible.map(id=>({getAttribute:()=>id}))];
        if(key==='textarea,[contenteditable="true"]'&&draft)return [{value:'unsent draft'}];
        return [];
      }
    };
    const fetch=async path=>{
      if(path==='/api/models'){
        calls.models++;
        return {ok:true,json:async()=>[{model_name:'kiro-byo-type:tiga:multi'},...available.map(model_name=>({model_name}))]};
      }
      if(path==='/api/chat/slots'){
        calls.slots++;
        return {ok:true,json:async()=>[{running,compacting:false,preparing:false,stopping:false,pending_approval:false,needs_input:false,wake_starting:false,queue_depth:0}]};
      }
      throw Error('unexpected network '+path);
    };
    vm.runInNewContext(source,{window,document,fetch,Date:{now:()=>now},
      Event:class{constructor(type){this.type=type;}},BroadcastChannel:class{constructor(){}},MutationObserver:class{observe(){}}});
    return {tick,window};
  }
  let page=mount();await settle();
  if(calls.reloads!==0)throw Error('baseline unexpectedly reloaded');
  // Gateway /api/models sees the newly added relay models; in React the
  // conversation option list still contains only the older provider models.
  available=['original-a','original-b','relay/claude-sonnet','relay/gpt-new'];
  if(scenario==='draft')draft=true;
  if(scenario==='active_turn')running=true;
  page.tick();await settle();
  if(scenario==='draft'||scenario==='active_turn'){
    if(calls.reloads!==0)throw Error('unsafe automatic refresh');
    draft=false;running=false;
    page.tick();await settle();
  }
  if(calls.reloads!==1)throw Error('same-Mode provider addition ignored '+JSON.stringify(calls));
  if(scenario==='saved_filter'){
    // A hard-coded user visible-model allowlist may still intentionally omit
    // the new provider after refresh. Never mutate saved settings or loop.
    for(let i=0;i<3;i++){
      now+=16000;
      page=mount();await settle();
    }
    if(calls.reloads!==1)throw Error('saved visible-model filter caused a false refresh '+JSON.stringify(calls));
  } else {
    visible=available;
    mount();await settle();
    if(calls.reloads!==1)throw Error('fresh model picker reloaded again');
  }
  console.log(scenario,JSON.stringify(calls));
})().catch(e=>{console.error(e);process.exitCode=1});
''',encoding='utf-8')
    result = subprocess.run(['node',str(harness),str(js),scenario],capture_output=True,text=True,timeout=16)
    assert result.returncode == 0, result.stdout+result.stderr


def test_r20_pricing_removed_but_root_v1_provider_fallback_retained() -> None:
    root=Path(__file__).resolve().parents[1]
    routes=(root/'backend/routes.py').read_text(encoding='utf-8')
    js=(root/'ui/index.mjs').read_text(encoding='utf-8')
    admin=(root/'src/kiro_byo/provider_admin.py').read_text(encoding='utf-8')
    assert not (root/'src/kiro_byo/relay_pricing.py').exists()
    assert '/providers/{provider_id}/pricing' not in routes
    assert 'fetch_public_pricing' not in routes
    assert '查询价格' not in js and 'fetchPricing' not in js
    assert '_discover_with_root_fallback' in admin
