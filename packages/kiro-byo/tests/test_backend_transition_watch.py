"""Real injected JS executed under a fake browser with two backend families."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_byo.crew11_model_labels import block


@pytest.mark.parametrize('direction', ['official_to_dyna', 'official_to_tiga', 'dyna_to_official', 'tiga_to_official'])
@pytest.mark.parametrize('scenario', ['idle', 'running', 'draft', 'slots_503', 'config_503'])
def test_backend_transition_refreshes_only_when_safe(tmp_path: Path, direction: str, scenario: str) -> None:
    if shutil.which('node') is None:
        pytest.skip('node is unavailable')
    match = re.search(r'<script[^>]*>(.*?)</script>', block(), re.S)
    assert match is not None
    script = tmp_path / 'injected.js'
    script.write_text(match.group(1), encoding='utf-8')
    runner = tmp_path / 'test.cjs'
    runner.write_text(r'''
const vm=require('node:vm'),fs=require('node:fs');
(async()=>{
  const direction=process.argv[3],scenario=process.argv[4];
  const initial=direction.split('_to_')[0],target=direction.split('_to_')[1];
  let state=initial,busy=scenario==='running',draft=scenario==='draft';
  let poll=null, reloads=0, modelCalls=0, configCalls=0;
  const window={setInterval:fn=>{poll=fn;return 1;},clearInterval:()=>{},
    addEventListener:()=>{},dispatchEvent:()=>{},requestAnimationFrame:()=>1,
    sessionStorage:{getItem:()=>null,setItem:()=>{}},location:{reload:()=>reloads++}};
  const chip={getAttribute:key=>key==='data-model-id'?
    (initial==='official'?'auto':'kiro-byo-type:'+initial+':'+(initial==='tiga'?'multi':'flash')):'',
    textContent:''};
  const document={body:null,visibilityState:'visible',addEventListener:()=>{},dispatchEvent:()=>{},
    querySelector:sel=>sel==='[data-testid="composer-model-chip"]'?chip:null,
    querySelectorAll:sel=>sel.startsWith('textarea,')&&draft?[{value:'unsent'}]:[]};
  const fetch=async path=>{
    if(path==='/api/models'){
      modelCalls++;
      return {ok:true,json:async()=>state==='official'?[{model_name:'auto'},{model_name:'official-v1'}]:
        [{model_name:'kiro-byo-type:'+state+':'+(state==='tiga'?'multi':'flash')}]} }
    if(path==='/api/config/kirocrew'){
      configCalls++;
      return {ok:!(scenario==='config_503' && state==='official'),json:async()=>
        ({agent:{acp_backend:state==='official'?'kiro':'kiro-byo'}})} }
    if(path==='/api/chat/slots'){
      return {ok:scenario!=='slots_503',json:async()=>[
        {running:busy,compacting:false,preparing:false,stopping:false,
          pending_approval:false,needs_input:false,wake_starting:false,queue_depth:0}]} }
    throw Error('Unexpected endpoint: '+path);
  };
  const src=fs.readFileSync(process.argv[2],'utf8');
  vm.runInNewContext(src,{window,document,fetch,Event:class{constructor(t){this.type=t}},
    BroadcastChannel:class{constructor(){}},MutationObserver:class{observe(){}}});
  const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
  await settle(); if(reloads!==0)throw Error('unexpected baseline reload '+reloads);
  state=target; poll(); await settle();
  const shouldReload=(scenario==='idle' || (scenario==='config_503' && target!=='official'));
  if(reloads!==Number(shouldReload))throw Error('wrong reload '+direction+'/'+scenario+'/'+reloads);
  if(scenario==='draft'||scenario==='running'){
    draft=false;busy=false;poll();await settle();
    if(reloads!==1)throw Error('never retried after becoming idle');
  }
  if(modelCalls<2)throw Error('no repeated catalogue probe');
  if(initial==='official' && configCalls<1)throw Error('no official backend verification');
  console.log(direction,scenario,'ok');
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding='utf-8')
    r = subprocess.run(['node', str(runner), str(script), direction, scenario], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stdout + r.stderr
