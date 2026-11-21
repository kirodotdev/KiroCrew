"""Real injected V5 browser script: server catalogue correct, React picker stale.

Node VM is a deterministic browser-contract harness, not Windows E2E.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from kiro_byo.crew11_model_labels import block


@pytest.mark.skipif(not shutil.which('node'), reason='Node unavailable')
@pytest.mark.parametrize('scenario', ['403_then_ok', 'busy_then_ok', 'draft_then_ok', 'retry_limit', 'manual_blocked'])
def test_stale_picker_safely_notifies_and_recovers(tmp_path: Path, scenario: str) -> None:
    src = re.search(r'<script[^>]*>(.*?)</script>', block(), re.S)
    assert src
    (tmp_path/'watch.js').write_text(src.group(1), encoding='utf8')
    (tmp_path/'watch.cjs').write_text(r'''
const vm=require('node:vm'),fs=require('node:fs');
(async()=>{
  const scenario=process.argv[3],source=fs.readFileSync(process.argv[2],'utf8');
  let tick, now=1700000000000, server='dyna', old='tiga', reloads=0;
  let gate=scenario==='busy_then_ok'?'busy':(scenario==='draft_then_ok'?'idle':'403');
  let draft=scenario==='draft_then_ok';
  const store=new Map();
  const element=(tag)=>({tag,style:{},children:[],parentNode:null,attrs:{},
    setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,cb){this[k+'Handler']=cb;},
    appendChild(node){this.children.push(node);node.parentNode=this;},
    removeChild(node){this.children=this.children.filter(c=>c!==node);node.parentNode=null;},
    querySelector(sel){if(sel==='[data-kiro-byo-sync-message]')return this.children.find(n=>n.attrs['data-kiro-byo-sync-message']!==undefined);return null;}});
  const body=element('body'),chip=element('chip');chip.getAttribute=k=>k==='data-model-id'?'kiro-byo-type:'+old+':multi':'';
  const option={getAttribute:()=> 'kiro-byo-type:'+old+':multi',closest:()=>({})};
  const window={setInterval:fn=>{tick=fn;return 1;},clearInterval:()=>{},addEventListener:()=>{},dispatchEvent:()=>{},requestAnimationFrame:()=>1,
    location:{reload(){reloads++;}},sessionStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)}};
  const document={body:null,visibilityState:'hidden',addEventListener:()=>{},dispatchEvent:()=>{},createElement:element,
    querySelector:sel=>sel==='[data-testid="composer-model-chip"]'?chip:null,
    querySelectorAll:sel=>sel.startsWith('[data-model-id^=')?[option]:(sel.startsWith('textarea,')&&draft?[{value:'unsent'}]:[])};
  const fetch=async path=>{
    if(path==='/api/models')return {ok:true,json:async()=>[{model_name:'kiro-byo-type:'+server+':flash'}]};
    if(path==='/api/chat/slots'){
      if(gate==='403')return {ok:false,status:403};
      return {ok:true,json:async()=>[{running:gate==='busy',queue_depth:0}]};
    }
    throw Error('unexpected '+path);
  };
  const FakeDate=class extends Date{static now(){return now;}};
  vm.runInNewContext(source,{window,document,fetch,Date:FakeDate,
    Event:class{constructor(t){this.type=t;}},BroadcastChannel:class{constructor(){}},MutationObserver:class{observe(){}}});
  if(typeof tick!=='function')throw Error('missing watcher');
  document.body=body;document.visibilityState='visible';
  const settle=async()=>{for(let i=0;i<25;i++)await new Promise(r=>setImmediate(r));};
  tick();await settle();
  const blocked=window.__kiroByoModeSync;
  if(!blocked || blocked.target!=='byo:dyna' || !blocked.blocked)throw Error('missing visible sync status '+JSON.stringify(blocked));
  if(reloads!==0)throw Error('unsafe reload');
  const banner=body.children.find(n=>n.id==='kiro-byo-model-sync-warning');
  if(!banner)throw Error('missing banner');
  if(scenario==='manual_blocked'){
    if(typeof banner.__action.clickHandler!=='function')throw Error('missing explicit user action');
    banner.__action.clickHandler();
    if(reloads!==1)throw Error('manual action did not reload');
  } else if (scenario==='retry_limit'){
    draft=false;gate='idle';tick();await settle();if(reloads!==1)throw Error('first safe retry');
    now+=16000;tick();await settle();if(reloads!==2)throw Error('second safe retry');
    now+=16000;tick();await settle();if(reloads!==2)throw Error('loop not bounded');
    if(window.__kiroByoModeSync.blocked!=='retry-limit')throw Error('no bounded retry warning');
  } else {
    if(scenario==='busy_then_ok' && blocked.blocked!=='busy')throw Error('no busy status');
    if(scenario==='draft_then_ok' && (blocked.blocked!=='draft'||banner.__action.style.display!=='none'))throw Error('draft guard failed');
    if(scenario==='403_then_ok' && blocked.blocked!=='slots-http-403')throw Error('no auth status');
    draft=false;gate='idle';tick();await settle();
    if(reloads!==1)throw Error('idle state did not auto-recover');
  }
  console.log('ok',scenario,reloads);
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding='utf8')
    result = subprocess.run(['node', str(tmp_path/'watch.cjs'),str(tmp_path/'watch.js'),scenario],
                            capture_output=True,text=True,timeout=12)
    assert result.returncode == 0, result.stdout+result.stderr
