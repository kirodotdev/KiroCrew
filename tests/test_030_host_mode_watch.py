"""Run the host-injected script in a simulated Crew 0.11 browser.

These are deterministic browser-contract tests, not Windows end-to-end tests.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_byo.crew11_model_labels import block


@pytest.mark.parametrize("direction", ["dyna_to_tiga", "tiga_to_dyna"])
@pytest.mark.parametrize("scenario", ["idle", "busy", "draft", "failed_slots", "mixed_catalogue", "provider_unavailable"])
def test_server_mode_change_refresh_is_guarded(tmp_path: Path, scenario: str, direction: str) -> None:
    if not shutil.which("node"):
        pytest.skip("Node unavailable")
    m = re.search(r'<script[^>]*>(.*?)</script>', block(), re.S)
    assert m is not None
    script = tmp_path / "dashboard.js"
    script.write_text(m.group(1), encoding="utf-8")
    harness = tmp_path / "mode-watch.cjs"
    harness.write_text(r'''
const vm = require('node:vm');
const fs = require('node:fs');
(async function main(){
  const scenario=process.argv[3];
  const listeners={};
  let tick;
  const calls={reloads:0, models:0, slots:0};
  let pack=process.argv[4]==='tiga_to_dyna'?'tiga':'dyna';
  let busy=scenario==='busy';
  let draft=scenario==='draft';
  const window={
    setInterval:fn=>{tick=fn; return 1;}, clearInterval:()=>{},
    addEventListener:(name,fn)=>{listeners[name]=fn;},
    dispatchEvent:()=>{}, requestAnimationFrame:()=>1,
    location:{reload:()=>{calls.reloads++;}}
  };
  const document={body:null,visibilityState:'visible',
    addEventListener:()=>{},dispatchEvent:()=>{},querySelector:()=>null,querySelectorAll:sel=>sel.startsWith('textarea,')&&draft?[{value:'unsent prompt'}]:[]};
  const fetch=async path=>{
    if(path==='/api/models') {
      calls.models++;
      if (scenario==='provider_unavailable' && pack==='tiga') return {ok:false};
      return {ok:true,json:async()=>scenario==='mixed_catalogue'&&pack==='tiga'
        ?[{model_name:'kiro-byo-type:dyna:flash'},{model_name:'kiro-byo-type:tiga:multi'}]
        :[{model_name:'kiro-byo-type:'+pack+':'+(pack==='tiga'?'multi':'flash')}]};
    }
    if(path==='/api/chat/slots') {
      calls.slots++;
      if (scenario==='failed_slots') return {ok:false};
      return {ok:true,json:async()=>[{key:'chat1',running:busy,compacting:false,preparing:false,
        stopping:false,pending_approval:false,needs_input:false,wake_starting:false,queue_depth:0}]};
    }
    throw Error('unexpected endpoint '+path);
  };
  const source=fs.readFileSync(process.argv[2],'utf8');
  vm.runInNewContext(source,{window,document,fetch,Event:class{constructor(type){this.type=type;}},
    BroadcastChannel:class{constructor(){}},MutationObserver:class{observe(){}}});
  const settle=async()=>{for(let i=0;i<18;i++)await new Promise(resolve=>setImmediate(resolve));};
  await settle();
  if(calls.reloads!==0)throw Error('baseline unexpectedly reloaded');
  if(typeof tick!=='function')throw Error('no host poll installed');
  pack=pack==='dyna'?'tiga':'dyna'; tick(); await settle();
  const shouldReload=scenario==='idle';
  if(calls.reloads!==(shouldReload?1:0))throw Error('unexpected action '+scenario+' '+JSON.stringify(calls));
  if(scenario==='busy' || scenario==='draft'){
    busy=false; draft=false; tick(); await settle();
    if(calls.reloads!==1)throw Error('pending transition not released at idle boundary');
  }
  if(calls.models<2)throw Error('catalogue never polled');
  console.log('case',scenario,'ok',JSON.stringify(calls));
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding="utf-8")
    proc = subprocess.run(["node", str(harness), str(script), scenario, direction], text=True,
                          capture_output=True, timeout=12)
    assert proc.returncode == 0, proc.stdout + proc.stderr

@pytest.mark.parametrize("initial_server,old_chip", [("dyna", "tiga"), ("tiga", "dyna")])
def test_closed_picker_on_first_page_load_detects_stale_type_and_avoids_reload_loop(
    tmp_path: Path, initial_server: str, old_chip: str
) -> None:
    """A cold page with a closed menu must not silently keep a stale composer Type.

    Uses the actual injected script with a persistent sessionStorage analogue.
    No React state is mutated; the only host action is one guarded reload.
    """
    if not shutil.which("node"):
        pytest.skip("Node unavailable")
    m = re.search(r'<script[^>]*>(.*?)</script>', block(), re.S)
    assert m is not None
    script = tmp_path / 'script.js'
    script.write_text(m.group(1), encoding='utf-8')
    runner = tmp_path / 'cold-chip.cjs'
    runner.write_text(r'''
const vm=require('node:vm'),fs=require('node:fs');
(async()=>{
  const server=process.argv[3],old=process.argv[4];
  const persisted=new Map();
  const calls={reloads:0,models:0,slots:0};
  const script=fs.readFileSync(process.argv[2],'utf8');
  let pageMode=old;
  const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
  function mount(){
    let tick;
    const window={
      setInterval:fn=>{tick=fn;return 1;},clearInterval:()=>{},
      addEventListener:()=>{},dispatchEvent:()=>{},requestAnimationFrame:()=>1,
      sessionStorage:{getItem:k=>persisted.get(k)||null,setItem:(k,v)=>persisted.set(k,v)},
      location:{reload:()=>{calls.reloads++;}}
    };
    const chip={getAttribute:key=>key==='data-model-id'?'kiro-byo-type:'+pageMode+':'+(pageMode==='tiga'?'multi':'flash'):'',textContent:''};
    const document={body:null,visibilityState:'visible',
      addEventListener:()=>{},dispatchEvent:()=>{},
      querySelector:key=>key==='[data-testid="composer-model-chip"]'?chip:null,
      querySelectorAll:()=>[]};
    const fetch=async(path)=>{
      if(path==='/api/models'){
        calls.models++;return {ok:true,json:async()=>[{model_name:'kiro-byo-type:'+server+':'+(server==='tiga'?'multi':'flash')}]} }
      if(path==='/api/chat/slots'){
        calls.slots++;return {ok:true,json:async()=>[{running:false,compacting:false,queue_depth:0}]} }
      throw Error('unexpected '+path);
    };
    vm.runInNewContext(script,{window,document,fetch,
      Event:class{constructor(type){this.type=type;}},BroadcastChannel:class{constructor(){}},
      MutationObserver:class{observe(){}}});
    return tick;
  }
  mount(); await settle();
  if(calls.reloads!==1 || calls.slots!==1)throw Error('closed chip not detected '+JSON.stringify(calls));
  mount(); await settle();
  if(calls.reloads!==1 || calls.slots!==1)throw Error('refresh loop '+JSON.stringify(calls));
  pageMode=server;mount();await settle();
  if(calls.reloads!==1)throw Error('matched chip reloaded');
  console.log(JSON.stringify(calls));
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding='utf-8')
    result = subprocess.run(["node", str(runner), str(script), initial_server, old_chip],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
